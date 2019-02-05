#!/usr/bin/env python
"""
Middleman

Middleman is responsible for monitoring for incoming submission requests,
sending submissions, waiting for submissions to complete, sending a message
to a notification queue as specified by the submission and, based on the
score received, possibly sending a message to indicate that an alert should
be created.
"""

import threading
import redis
import json
import uuid
import signal
from datetime import datetime, timedelta
from math import tanh
from random import random

from assemblyline.common.str_utils import dotdump, safe_str
from assemblyline.common.exceptions import get_stacktrace_info
from assemblyline.common.isotime import iso_to_epoch, now, now_as_iso
from assemblyline.common.importing import load_module_by_path
# from assemblyline.common.net import get_hostip, get_hostname, get_mac_for_ip
from assemblyline.common import net
from assemblyline.common import forge

from assemblyline import odm

from assemblyline.remote.datatypes.exporting_counter import AutoExportingCounters
from assemblyline.remote.datatypes.queues.named import NamedQueue
from assemblyline.remote.datatypes.queues.priority import PriorityQueue
from assemblyline.remote.datatypes.queues.comms import CommsQueue
from assemblyline.remote.datatypes.queues.multi import MultiQueue
from assemblyline.remote.datatypes.hash import Hash
from assemblyline.remote.datatypes import get_client
from assemblyline.odm.models.submission import Submission, SubmissionParams

from dispatching.submission_tool import SubmissionTool


_completeq_name = 'm-complete'
_ingestq_name = 'm-ingest'
_dup_prefix = 'w-m-'
_min_priority = 1
_max_retries = 10
_retry_delay = 180
_max_time = 2 * 24 * 60 * 60  # Wait 2 days for responses.


def install_interrupt_handler(handler):
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def drop_chance(length, maximum):
    return tanh(float(length - maximum) / maximum * 2.0)


###############################################################################
#
# To calculate the probability of dropping an incoming submission we compare
# the number returned by random() which will be in the range [0,1) and the
# number returned by tanh() which will be in the range (-1,1).
#
# If length is less than maximum the number returned by tanh will be negative
# and so drop will always return False since the value returned by random()
# cannot be less than 0.
#
# If length is greater than maximum, drop will return False with a probability
# that increases as the distance between maximum and length increases:
#
#     Length           Chance of Dropping
#
#     <= maximum       0
#     1.5 * maximum    0.76
#     2 * maximum      0.96
#     3 * maximum      0.999
#
###############################################################################
def must_drop(length, maximum):
    return random() < drop_chance(length, maximum)

# def seconds(t, default=0):
#     try:
#         try:
#             return float(t)
#         except ValueError:
#             return iso_to_epoch(t)
#     except:
#         return default


def determine_resubmit_selected(selected, resubmit_to):
    resubmit_selected = None

    selected = set(selected)
    resubmit_to = set(resubmit_to)

    if not selected.issuperset(resubmit_to):
        resubmit_selected = sorted(selected.union(resubmit_to))

    return resubmit_selected


def should_resubmit(score):

    # Resubmit:
    #
    # 100%     with a score above 400.
    # 10%      with a score of 301 to 400.
    # 1%       with a score of 201 to 300.
    # 0.1%     with a score of 101 to 200.
    # 0.01%    with a score of 1 to 100.
    # 0.001%   with a score of 0.
    # 0%       with a score below 0.

    if score < 0:
        return False

    if score > 400:
        return True

    resubmit_probability = 1.0 / 10 ** ((500 - score) / 100)

    return random() < resubmit_probability


# TODO move to config file
@odm.model()
class Ingest(odm.Model):
    # The ingest defaults are the values given to ingest tasks when no input
    # has been provided
    default_user = odm.Keyword()
    default_services = odm.List(odm.Keyword(), default=['Antivirus', 'Extraction', 'Filtering',
                                                        'Networking', 'Static Analysis'])
    default_resubmit_services = odm.List(odm.Keyword(), default=['Dynamic Analysis'])

    # Maximum permitted length of metadata values
    max_value_size = odm.Integer()

    # Maximum file size for ingestion
    max_size = odm.Integer()


@odm.model()
class IngestTask(odm.Model):
    # Submission Parameters
    params = odm.Compound(SubmissionParams)

    # Information about the ingestion itself, parameters irrelivant to the
    # system outside of middleman
    ingest_time = odm.Date()
    scan_key = odm.Keyword(default_set=True)  # the filescore key
    retries = odm.Integer(default=0)
    retry_at = odm.Date(default=0)
    notification_queue = odm.Keyword(default='')
    # If the ingestion has failed for some reason, what is it?
    failure = odm.Text(default='')

    # describe the file being ingested
    sha256 = odm.Keyword()
    file_size = odm.Integer()
    filename = odm.Keyword(default='')
    classification = odm.Keyword()
    metadata = odm.Mapping(odm.Keyword())
    score = odm.Float(default=float('NaN'))  # Score from previous processing of this file


class Middleman:
    """Internal interface to the ingestion queues."""

    def __init__(self, datastore, logger, classification=None):
        self.datastore = datastore
        self.log = logger

        # Cache the user groups
        self._user_groups = {}
        self.cache = {}
        self.cache_lock = threading.RLock()  # TODO are middle man instances single threaded now?
        self.whitelisted = {}
        self.whitelisted_lock = threading.RLock()
        self.running = True

        # Create a config cache that will refresh config values periodically
        self.config = forge.CachedObject(forge.get_config)

        # TODO Should any of these values be read dynamically
        self.is_low_priority = load_module_by_path(self.config.core.middleman.is_low_priority)
        self.get_whitelist_verdict = load_module_by_path(self.config.core.middleman.get_whitelist_verdict)
        self.whitelist = load_module_by_path(self.config.core.middleman.whitelist)

        # Constants are loaded based on a non-constant path, so has to be done at init rather than load
        constants = forge.get_constants(self.config)
        self.priority_value = constants.PRIORITIES
        self.priority_range = constants.PRIORITY_RANGES
        self.threshold_value = constants.PRIORITY_THRESHOLDS

        # Connect to the redis servers
        self.redis = get_client(
            db=self.config.core.redis.nonpersistent.db,
            host=self.config.core.redis.nonpersistent.host,
            port=self.config.core.redis.nonpersistent.port,
            private=False,
        )
        self.persistent_redis = get_client(
            db=self.config.core.redis.persistent.db,
            host=self.config.core.redis.persistent.host,
            port=self.config.core.redis.persistent.port,
            private=False,
        )

        # Classification engine
        self.ce = classification or forge.get_classification()

        self.ingester_counts = AutoExportingCounters(
            name='ingester',
            host=net.get_hostip(),
            auto_flush=True,
            auto_log=False,
            export_interval_secs=self.config.logging.export_interval,
            channel=forge.get_metrics_sink())

        self.whitelister_counts = AutoExportingCounters(
            name='whitelister',
            host=net.get_hostip(),
            auto_flush=True,
            auto_log=False,
            export_interval_secs=self.config.logging.export_interval,
            channel=forge.get_metrics_sink())

        # State. The submissions in progress are stored in Redis in order to
        # persist this state and recover in case we crash.
        self.scanning = Hash('m-scanning-table', self.persistent_redis)

        # Input. An external process creates a record when any submission completes.
        self.complete_queue = NamedQueue(_completeq_name, self.redis)

        # Internal. Dropped entries are placed on this queue.
        self.drop_queue = NamedQueue('m-drop', self.persistent_redis)

        # Input. An external process places submission requests on this queue.
        self.ingest_queue = NamedQueue(_ingestq_name, self.persistent_redis)

        # Traffic (TODO: What traffic?)
        self.traffic_queue = CommsQueue('traffic', self.redis)

        # Input/Output. Unique requests are placed in and processed from this queue.
        self.unique_queue = PriorityQueue('m-unique', self.persistent_redis)

        # Internal, delay queue for retrying
        self.retry_queue = NamedQueue('m-retry', self.persistent_redis)

        # Internal, timeout watch queue
        self.timeout_queue = PriorityQueue('m-timeout', self.redis)

        # Internal, queue for processing duplicates
        self.duplicate_queue = MultiQueue(self.persistent_redis)

        self.client = SubmissionTool(datastore=self.datastore,
                                     transport=None,
                                     redis=self.redis)

    def start(self):
        """Start shared middleman auxillary components."""
        self.ingester_counts.start()
        self.whitelister_counts.start()
        install_interrupt_handler(self.interrupt_handler)

    def interrupt_handler(self, *_):
        self.log.info("Caught signal. Coming down...")
        self.running = False
        self.stop()

    def stop(self):
        """Stop shared middleman auxillary components."""
        self.ingester_counts.stop()
        self.whitelister_counts.stop()

    def get_user_groups(self, user):
        groups = self._user_groups.get(user, None)
        if groups is None:
            ruser = self.datastore.user.get(user)
            if not ruser:
                return None
            groups = ruser.get('groups', [])
            self._user_groups[user] = groups
        return groups

    def ingest(self, task: IngestTask):
        # Load a snapshot of ingest parameters as of right now.
        # self.config is a timed cache
        conf = self.config.core.middleman
        max_file_size = self.config.submission.max_file_size
        param = task.params

        # ... and groups.
        if not param.groups:
            groups = self.get_user_groups(param.submitter)
            if groups is None:
                error_message = f"User not found [{param.submitter}] ingest failed"
                self.send_notification(task, failure=error_message, logfunc=self.log.warning)
                return
            param.groups = groups

        self.ingester_counts.increment('ingest.bytes_ingested', task.file_size)
        self.ingester_counts.increment('ingest.submissions_ingested')

        if not task.sha256:
            self.send_notification(task, failure="Invalid sha256", logfunc=self.log.warning)
            return

        if not self.ce.is_valid(task.classification):
            _message = f"Invalid classification {task.classification}"
            self.send_notification(task, failure=_message, logfunc=self.log.warning)
            return

        if not self.ce.is_valid(param.classification):
            _message = f"Invalid classification {param.classification}"
            self.send_notification(task, failure=_message, logfunc=self.log.warning)
            return

        # Clean up metadata strings, since we may delete some, iterate on a copy of the keys
        for key in list(task.metadata.keys()):
            value = task.metadata[key]
            meta_size = len(value)
            if meta_size > self.config.submission.max_metadata_length:
                self.log.info(f'Removing {key} from {task.sha256} from {param.submitter}')
                task.metadata.pop(key)

        if task.file_size > max_file_size and not task.params.ignore_size and not task.params.never_drop:
            task.failure = f"File too large ({task.file_size} > {max_file_size})"
            self.drop_queue.push(task.json())
            self.ingester_counts.increment('ingest.skipped')
            return

        pprevious, previous, score = None, False, None
        if not param.ignore_cache:
            pprevious, previous, score, _ = self.check(task)

        # Assign priority.
        low_priority = self.is_low_priority(task)

        priority = param.priority
        if priority < 0:
            priority = self.priority_value['medium']

            if score is not None:
                priority = self.priority_value['low']
                for level, threshold in self.threshold_value.items():
                    if score >= threshold:
                        priority = self.priority_value[level]
                        break
            elif low_priority:
                priority = self.priority_value['low']

        # Reduce the priority by an order of magnitude for very old files.
        current_time = now()
        if priority and self.expired(current_time - task.ingest_time.timestamp(), 0):
            priority = (priority / 10) or 1

        param.priority = priority

        # Do this after priority has been assigned.
        # (So we don't end up dropping the resubmission).
        if previous:
            self.ingester_counts.increment('ingest.duplicates')
            self.finalize(pprevious, previous, score, task)
            return

        if self.drop(task):
            return

        if self.is_whitelisted(task):
            return

        self.unique_queue.push(priority, task.json())

    def check(self, task: IngestTask):
        key = self.stamp_filescore_key(task)

        with self.cache_lock:
            result = self.cache.get(key, None)

        counter_name = 'ingest.cache_hit_local'
        if result:
            self.log.info('Local cache hit')
        else:
            counter_name = 'ingest.cache_hit'

            result = self.datastore.filescore.get(key)
            if result:
                self.log.info('Remote cache hit')
            else:
                self.ingester_counts.increment('ingest.cache_miss')
                return None, False, None, key

            self.cache[key] = {
                'errors': result.errors,
                'psid': result.psid,
                'score': result.score,
                'sid': result.sid,
                'time': result.time,
            }

        current_time = now()
        delta = current_time - result.time
        errors = result.errors

        if self.expired(delta, errors):
            self.ingester_counts.increment('ingest.cache_expired')
            self.cache.pop(key, None)
            self.datastore.filescore.delete(key)
            return None, False, None, key
        elif self.stale(delta, errors):
            self.ingester_counts.increment('ingest.cache_stale')
            return None, False, result.score, key

        self.ingester_counts.increment(counter_name)

        return result.psid, result.sid, result.score, key

    def stale(self, delta: float, errors: int):
        if errors:
            return delta >= self.config.core.middleman.incomplete_stale_after_seconds
        else:
            return delta >= self.config.core.middleman.stale_after_seconds

    def stamp_filescore_key(self, task: IngestTask, sha256=None):
        if not sha256:
            sha256 = task.sha256

        selected = task.params.services.selected
        key = task.scan_key

        if not key:
            key = task.params.create_filescore_key(sha256, selected)
            task.scan_key = key

        return key

    def completed(self, task):
        """Invoked when notified that a submission has completed."""
        sha256 = task.root_sha256

        psid = task.psid
        score = task.score
        sid = task.sid

        scan_key = task.scan_key

        with ScanLock(scan_key):
            # Remove the entry from the hash of submissions in progress.
            raw = scanning.pop(scan_key)  # df pull pop
            if not raw:
                logger.warning("Untracked submission (score=%d) for: %s %s",
                               int(score), sha256, str(task.metadata))

                # Not a result we care about. We are notified for every
                # submission that completes. Some submissions will not be ours.
                if task.metadata:
                    stype = None
                    try:
                        stype = task.metadata.get('type', None)
                    except:  # pylint: disable=W0702
                        logger.exception("Malformed metadata: %s:", sid)

                    if not stype:
                        return scan_key

                    if (task.description or '').startswith(default_prefix):
                        raw = {
                            'metadata': task.metadata,
                            'overrides': get_submission_overrides(task, overrides),
                            'sha256': sha256,
                            'type': stype,
                        }

                        finalize(psid, sid, score, Notice(raw))
                return scan_key

            errors = task.raw.get('error_count', 0)
            file_count = task.raw.get('file_count', 0)
            self.ingester_counts.increment('ingest.submissions_completed')
            self.ingester_counts.increment('ingest.files_completed', file_count)
            self.ingester_counts.increment('ingest.bytes_completed', int(task.size or 0))

            with self.cache_lock:
                cache[key] = {
                    'errors': errors,
                    'psid': psid,
                    'score': score,
                    'sid': sid,
                    'time': now(),
                }

            finalize(psid, sid, score, notice)  # df push calls

            def exhaust():
                while True:
                    res = dupq.pop(  # df pull pop
                        dup_prefix + scan_key, blocking=False
                    )
                    if res is None:
                        break
                    yield res

            # You may be tempted to remove the assignment to dups and use the
            # value directly in the for loop below. That would be a mistake.
            # The function finalize may push on the duplicate queue which we
            # are pulling off and so condensing those two lines creates a
            # potential infinite loop.
            dups = [dup for dup in exhaust()]
            for dup in dups:
                finalize(psid, sid, score, Notice(dup))

        return scan_key

    def send_notification(self, task: IngestTask, failure=None, logfunc=None):
        if logfunc is None:
            logfunc = self.log.info

        if failure:
            task.failure = failure

        failure = task.failure
        if failure:
            logfunc("%s: %s", failure, str(task.json()))

        if not task.notification_queue:
            return

        threshold = task.notification_threshold
        if threshold is not None and task.score is not None and task.score < threshold:
            return

        q = notificationq.get(task.notification_queue, None)
        if not q:
            notificationq[task.notification_queue] = q = \
                queue.NamedQueue(task.notification_queue, **persistent)
        q.push(task.raw)


    def expired(self, delta, errors):
        # incomplete_expire_after_seconds = 3600

        if errors:
            return delta >= self.config.core.middleman.incomplete_expire_after_seconds
        else:
            return delta >= self.config.core.middleman.expire_after

    def drop(self, task: IngestTask) -> bool:
        priority = task.params.priority
        sample_threshold = self.config.core.middleman.sampling_at

        dropped = False
        if priority <= _min_priority:
            dropped = True
        else:
            for level, rng in self.priority_range.items():
                if rng[0] <= priority <= rng[1] and level in sample_threshold:
                    dropped = must_drop(self.unique_queue.count(*rng), sample_threshold[level])
                    break

            if not dropped:
                if task.file_size > self.config.submission.max_file_size or task.file_size == 0:
                    dropped = True

        if task.params.never_drop or not dropped:
            return False

        task.failure = 'Skipped'
        self.drop_queue.push(task.json())

        self.ingester_counts.increment('ingest.skipped')

        return True

    def is_whitelisted(self, task: IngestTask):
        reason, hit = self.get_whitelist_verdict(self.whitelist, task)
        hit = {x: dotdump(safe_str(y)) for x, y in hit.items()}

        if not reason:
            with self.whitelisted_lock:
                reason = self.whitelisted.get(task.sha256, None)
                if reason:
                    hit = 'cached'

        if reason:
            if hit != 'cached':
                with self.whitelisted_lock:
                    self.whitelisted[task.sha256] = reason

            task.failure = "Whitelisting due to reason %s (%s)" % (dotdump(safe_str(reason)), hit)
            self.drop_queue.push(task.json())

            self.ingester_counts.increment('ingest.whitelisted')
            self.whitelister_counts.increment('whitelist.' + reason)

        return reason

    def submit(self, task: IngestTask):

        self.client.submit(
            sha256=task.sha256,
            path=task.filename or task.sha256,
            metadata=task.metadata,
            params=task.params
        )

        self.timeouts.push(now(_max_time), task.scan_key)

    def retry(self, task, scan_key, ex):
        current_time = now()

        retries = task.retries + 1

        if retries > _max_retries:
            trace = ''
            if ex:
                trace = ': ' + get_stacktrace_info(ex)
            self.log.error('Max retries exceeded for %s%s', task.sha256, trace)
            self.duplicate_queue.delete(_dup_prefix + scan_key)
        elif self.expired(current_time - task.ingest_time.timestamp(), 0):
            self.log.info('No point retrying expired submission for %s', task.sha256)
            self.duplicate_queue.delete(_dup_prefix + scan_key)
        else:
            self.log.info('Requeuing %s (%s)', task.sha256, ex or 'unknown')
            task.retries = retries
            task.retry_at = now(_retry_delay)
            self.retry_queue.push(task.json())

    def finalize(self, psid, sid, score, task: IngestTask):
        self.log.debug("Finalizing (score=%d) %s", score, task.sha256)
        if psid:
            task.params.psid = psid
        task.score = score

        selected = task.params.services.selected
        resubmit_to = task.params.services.resubmit

        resubmit_selected = determine_resubmit_selected(selected, resubmit_to)
        will_resubmit = resubmit_selected and should_resubmit(score)
        if will_resubmit:
            task.params.psid = ''

        if self.is_alert(task, score):
            obj = task.as_primitives()
            obj['sid'] = sid
            self.alert_queue.push(json.dumps(obj))

        self.send_notification(task)

        if will_resubmit:
            task.params.psid = sid
            task.resubmit_to = []
            task.scan_key = None
            task.selected = resubmit_selected

            self.unique_queue.push(task.priority, task.json())

    def is_alert(self, task: IngestTask, score):
        if not task.params.generate_alert:
            return False

        if score < self.threshold_value['critical']:
            return False

        return True

# # Globals
# alertq = queue.NamedQueue('m-alert', **persistent)  # df line queue
# chunk_size = 1000
# date_fmt = '%Y-%m-%dT%H:%M:%SZ'
# ip = get_hostip()
# hostinfo = {
#     'ip:': ip,
#     'mac_address': get_mac_for_ip(ip),
#     'host': get_hostname(),
# }

# running = True
# sampling = False

# start_time = now()
# submissionq = queue.NamedQueue('m-submission-' + shard, **persistent)  # df line queue
# timeouts = []
# timeouts_lock = RLock()
#
# dropper_threads = 1
# try:
#     dropper_threads = int(config.core.middleman.dropper_threads)
# except AttributeError:
#     logger.warning(
#         "No dropper_threads setting. Defaulting to %d.",
#         dropper_threads
#     )
#
#
# ingester_threads = 1
# try:
#     ingester_threads = int(config.core.middleman.ingester_threads)
# except AttributeError:
#     logger.warning(
#         "No ingester_threads setting. Defaulting to %d.",
#         ingester_threads
#     )
#
# submitter_threads = 1
# try:
#     submitter_threads = int(config.core.middleman.submitter_threads)
# except AttributeError:
#     logger.warning(
#         "No submitter_threads setting. Defaulting to %d.",
#         submitter_threads
#     )
#
#

#
# # Output. Notifications are placed on a notification queue.
# notificationq = {}
#
#
# # Status.
# statusq = queue.CommsQueue('status')
#



#
# # noinspection PyBroadException
# def init():
#     datastore = forge.get_datastore()
#     datastore.commit_index('submission')
#
#     sids = [
#         x['submission.sid'] for x in datastore.stream_search(
#             'submission',
#             'state:submitted AND times.submitted:[NOW-1DAY TO *] '
#             'AND submission.metadata.type:* '
#             'AND NOT submission.description:Resubmit*'
#         )
#     ]
#
#     submissions = {}
#     submitted = {}
#     for submission in datastore.get_submissions(sids):
#         task = Task(submission)
#
#         if not task.original_selected or not task.root_sha256 or not task.scan_key:
#             continue
#
#         if forge.determine_ingest_queue(task.root_sha256) != ingestq_name:
#             continue
#
#         scan_key = task.scan_key
#         submissions[task.sid] = submission
#         submitted[scan_key] = task.sid
#
#     # Outstanding is the set of things Riak believes are being scanned.
#     outstanding = set(submitted.keys())
#
#     # Keys is the set of things middleman believes are being scanned.
#     keys = set(scanning.keys())
#
#     # Inflight is the set of submissions middleman and Riak agree are inflight.
#     inflight = outstanding.intersection(keys)
#
#     # Missing is the set of submissions middleman thinks are in flight but
#     # according to Riak are not incomplete.
#     missing = keys.difference(inflight)
#
#     # Process the set of submissions Riak believes are incomplete but
#     # middleman doesn't know about.
#     for scan_key in outstanding.difference(inflight):
#         sid = submitted.get(scan_key, None)
#
#         if not sid:
#             logger.info("Init: No sid found for incomplete")
#             continue
#
#         submission = submissions[sid]
#         task = Task(submission)
#
#         if not task.original_selected or not task.root_sha256 or not task.scan_key:
#             logger.info("Init: Not root_sha256 or original_selected")
#             continue
#
#         if not task.metadata:
#             logger.info(
#                 "Init: Incomplete submission is not one of ours: %s", sid
#             )
#
#         stype = None
#         try:
#             stype = task.metadata.get('type', None)
#         except:  # pylint: disable=W0702
#             logger.exception(
#                 "Init: Incomplete submission has malformed metadata: %s", sid
#             )
#
#         if not stype:
#             logger.info("Init: Incomplete submission missing type: %s", sid)
#
#         raw = {
#             'metadata': task.metadata,
#             'overrides': get_submission_overrides(task, overrides),
#             'sha256': task.root_sha256,
#             'type': stype,
#         }
#         raw['overrides']['selected'] = task.original_selected
#
#         reinsert(datastore, " (incomplete)", Notice(raw), logger)
#
#     r = redis.StrictRedis(persistent['host'],
#                           persistent['port'],
#                           persistent['db'])
#
#     # Duplicates is the set of sha256s where a duplicate queue exists.
#     duplicates = [
#         x.replace(dup_prefix, '', 1) for x in r.keys(dup_prefix + '*')
#     ]
#
#     # Process the set of duplicates where no scanning or riak entry exists.
#     for scan_key in set(duplicates).difference(outstanding.union(keys)):
#         raw = dupq.pop(dup_prefix + scan_key, blocking=False)
#         if not raw:
#             logger.warning("Init: Couldn't pop off dup queue (%s)", scan_key)
#             dupq.delete(dup_prefix + scan_key)
#             continue
#
#         reinsert(datastore, " (missed duplicate)", Notice(raw), logger)
#
#     while True:
#         res = completeq.pop(blocking=False)
#         if not res:
#             break
#
#         scan_key = completed(Task(res))
#         try:
#             missing.remove(scan_key)
#         except:  # pylint: disable=W0702
#             pass
#
#     # Process the set of submissions middleman thinks are in flight but
#     # according to Riak are not incomplete.
#     for scan_key in missing:
#         raw = scanning.pop(scan_key)
#         if raw:
#             reinsert(datastore, '', Notice(raw), logger, retry_all=False)
#
#     # Set up time outs for all inflight submissions.
#     expiry_time = now(max_time)
#     for scan_key in inflight:
#         # No need to lock. We're the only thing running at this point.
#         timeouts.append(Timeout(scan_key, expiry_time))
#
#
#     datastore.close()
#
#
#
#
# # noinspection PyBroadException
# def process_timeouts():  # df node def
#     global timeouts  # pylint:disable=W0603
#
#     with timeouts_lock:
#         current_time = now()
#         index = 0
#
#         for t in timeouts:
#             if t.time >= current_time:
#                 break
#
#             index += 1
#
#             try:
#                 timed_out(t.scan_key)  # df push calls
#             except:  # pylint: disable=W0702
#                 logger.exception("Problem timing out %s:", t.scan_key)
#
#         timeouts = timeouts[index:]
#
#
# def reinsert(datastore, msg, notice, out, retry_all=True):
#     sha256 = notice.get('sha256')
#     if not sha256:
#         logger.error("Invalid sha256: %s", notice.raw)
#
#     if forge.determine_ingest_queue(sha256) != ingestq_name:
#         return
#
#     pprevious, previous, score = None, False, None
#     if not notice.get('ignore_cache', False):
#         pprevious, previous, score, _ = check(datastore, notice)
#
#     if previous:
#         out.info("Init: Found%s: %s", msg, notice.get('sha256'))
#         finalize(pprevious, previous, score, notice)
#     elif retry_all or not score:
#         logger.info("Init: Retrying%s: %s", msg, notice.get('sha256'))
#         ingestq.push(notice.raw)
#     else:
#         logger.info("Init: Stale%s: %s", msg, notice.get('sha256'))
#
#
#
# def return_exception(func, *args, **kwargs):
#     try:
#         func(*args, **kwargs)
#         return None
#     except Exception as ex:  # pylint: disable=W0703
#         return ex
#
#
