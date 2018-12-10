import time
import mock
import json

from models import Submission, Result, build_result_key
import dispatch_file
from dispatch_file import service_queue_name


class Error:
    def __init__(self, data, docid):
        self.id = docid


class MockFactory:
    def __init__(self, mock_type):
        self.type = mock_type
        self.mocks = {}

    def __call__(self, name, *args):
        if name not in self.mocks:
            self.mocks[name] = self.type(name, *args)
        return self.mocks[name]

    def __getitem__(self, name):
        return self.mocks[name]

    def __len__(self):
        return len(self.mocks)

    def flush(self):
        self.mocks.clear()


class MockDispatchHash:
    def __init__(self, *args):
        self._dispatched = {}
        self._finished = {}

    @staticmethod
    def _key(file_hash, service):
        return f"{file_hash}_{service}"

    def all_finished(self):
        return len(self._dispatched) == 0

    def finished(self, file_hash, service):
        return self._key(file_hash, service) in self._finished

    def dispatch_time(self, file_hash, service):
        return self._dispatched.get(self._key(file_hash, service), 0)

    def dispatch(self, file_hash, service):
        self._dispatched[self._key(file_hash, service)] = time.time()

    def finish(self, file_hash, service, result_key):
        key = self._key(file_hash, service)
        self._finished[key] = result_key
        self._dispatched.pop(key, None)

    def fail_dispatch(self, file_hash, service):
        self._dispatched[self._key(file_hash, service)] = 0


class MockCollection:
    def __init__(self):
        self._docs = {}
        self.next_searches = []

    def get(self, key):
        return self._docs[key]

    def exists(self, key):
        print('exists', key, self._docs, key in self._docs)
        return key in self._docs

    def save(self, key, doc):
        self._docs[key] = doc

    def search(self, query, fl=None, rows=None):
        if self.next_searches:
            return self.next_searches.pop(0)
        return {
            'items': [],
            'total': 0,
            'offset': 0,
            'rows': 0
        }


class MockDatastore:
    def __init__(self):
        self._collections = {}

    def __getattr__(self, name):
        if name not in self._collections:
            self._collections[name] = MockCollection()
        return self._collections[name]


class MockQueue:
    def __init__(self, *args, **kwargs):
        self.queue = []

    def push(self, obj):
        self.queue.append(obj)

    def length(self):
        return len(self.queue)

    def __len__(self):
        return len(self.queue)


class ConfigShim:
    def __init__(self, *args, **kwargs):
        pass

    def build_schedule(self, *args):
        return [
            ['extract', 'wrench'],
            ['av-a', 'av-b', 'frankenstrings'],
            ['xerox']
        ]

    def build_service_config(self, service, submission):
        return {}

    def service_timeout(self, service):
        return 60*10

    def service_failure_limit(self, service):
        return 4


def test_dispatcher():
    with mock.patch('dispatch_file.NamedQueue', MockFactory(MockQueue)) as mq:
        with mock.patch('dispatch_file.DispatchHash', MockFactory(MockDispatchHash)) as dh:
            with mock.patch('dispatch_file.ConfigManager', ConfigShim):
                ds = MockDatastore()
                file_hash = 'totally-a-legit-hash'
                ds.submissions.save('first-submission', Submission({'files': []}))

                dispatcher = dispatch_file.FileDispatcher(ds, tuple())
                print('==== first dispatch')
                # Submit a problem, and check that it gets added to the dispatch hash
                # and the right service queues
                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].dispatch_time(file_hash, 'extract') > 0
                assert dh['first-submission'].dispatch_time(file_hash, 'wrench') > 0
                assert len(mq[service_queue_name('extract')]) == 1
                assert len(mq[service_queue_name('wrench')]) == 1
                assert len(mq) == 3

                # Making the same call again should have no effect
                print('==== second dispatch')
                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].dispatch_time(file_hash, 'extract') > 0
                assert dh['first-submission'].dispatch_time(file_hash, 'wrench') > 0
                assert len(mq[service_queue_name('extract')]) == 1
                assert len(mq[service_queue_name('wrench')]) == 1
                print(mq.mocks)
                assert len(mq) == 3

                # Push back the timestamp in the dispatch hash to simulate a timeout,
                # make sure it gets pushed into that service queue again
                print('==== third dispatch')
                mq.flush()
                dh['first-submission'].fail_dispatch(file_hash, 'extract')

                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].dispatch_time(file_hash, 'extract') > 0
                assert dh['first-submission'].dispatch_time(file_hash, 'wrench') > 0
                assert len(mq[service_queue_name('extract')]) == 1
                assert len(mq) == 1

                # Mark extract as finished in the dispatch table, add a result object
                # for the wrench service, it should move to the second batch of services
                print('==== fourth dispatch')
                mq.flush()
                dh['first-submission'].finish(file_hash, 'extract', 'result-key')
                wrench_result_key = build_result_key(file_hash, 'wrench', {})
                print('wrench result key', wrench_result_key)
                ds.results.save(wrench_result_key, {})

                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].finished(file_hash, 'extract')
                assert dh['first-submission'].finished(file_hash, 'wrench')
                assert len(mq[service_queue_name('av-a')]) == 1
                assert len(mq[service_queue_name('av-b')]) == 1
                assert len(mq[service_queue_name('frankenstrings')]) == 1
                assert len(mq) == 3

                # Have the first AV fail, due to 'terminal' error, the next fail due to
                # too many timeout errors, frankenstrings finishes
                print('==== fifth dispatch')
                mq.flush()
                ds.errors.next_searches.append({'items': [Error({}, docid='error_key')]})
                ds.errors.next_searches.append({'items': []})
                ds.errors.next_searches.append({'total': 5})
                dh['first-submission'].finish(file_hash, 'frankenstrings', 'result-key')

                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].finished(file_hash, 'av-a')
                assert dh['first-submission'].finished(file_hash, 'av-b')
                assert dh['first-submission'].finished(file_hash, 'frankenstrings')
                assert len(mq[service_queue_name('xerox')]) == 1
                assert len(mq) == 1

                # Finish the xerox service and check if the submission completion got checked
                print('==== sixth dispatch')
                mq.flush()
                dh['first-submission'].finish(file_hash, 'xerox', 'result-key')

                dispatcher.handle(json.dumps({
                    'sid': 'first-submission',
                    'file_hash': file_hash,
                    'file_type': 'unknown',
                    'depth': 0
                }))

                assert dh['first-submission'].finished(file_hash, 'xerox')
                assert len(dispatcher.submission_queue) == 1
