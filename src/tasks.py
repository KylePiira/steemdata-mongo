import inspect
import os

from celery import Celery

from methods import (
    update_account,
    update_account_ops_quick,
    upsert_comment_chain,
    find_latest_item,
)
from mongostorage import (
    MongoStorage,
    DB_NAME,
    MONGO_HOST,
    MONGO_PORT,
)
from utils import log_exceptions, time_delta

# override a node for perf reasons
_custom_node = False


def new_celery(worker_name: str):
    return Celery(worker_name,
                  backend=os.getenv('CELERY_BACKEND_URL', 'redis://localhost:6379/0'),
                  broker=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'))


def caller_name(skip=7):
    """Get a name of a caller in the format module.class.method

       `skip` specifies how many levels of stack to skip while getting caller
       name. skip=1 means "who calls me", skip=2 "who calls my caller" etc.

       An empty string is returned if skipped levels exceed stack height
    """
    stack = inspect.stack()
    start = 0 + skip
    if len(stack) < start + 1:
        return ''
    parentframe = stack[start][0]

    name = []
    module = inspect.getmodule(parentframe)
    # `modname` can be None when frame is executed directly in console
    # TODO(techtonik): consider using __main__
    if module:
        name.append(module.__name__)
    # detect classname
    if 'self' in parentframe.f_locals:
        # I don't know any way to detect call from the object method
        # XXX: there seems to be no way to detect static method call - it will
        #      be just a function call
        name.append(parentframe.f_locals['self'].__class__.__name__)
    codename = parentframe.f_code.co_name
    if codename != '<module>':  # top level usually
        name.append(codename)  # function or a method
    del parentframe
    return ".".join(name)


# only run this code from celery worker
# we don't want to do global overrides for other processes
if str(caller_name()) != '__main__':
    mongo = MongoStorage(db_name=os.getenv('DB_NAME', DB_NAME),
                         host=os.getenv('DB_HOST', MONGO_HOST),
                         port=os.getenv('DB_PORT', MONGO_PORT))

# task definitions
# ----------------
tasks = new_celery('tasks')


@tasks.task
def update_account_async(account_name, load_extras=False):
    update_account(mongo, account_name, load_extras=load_extras)
    update_account_ops_quick(mongo, account_name)


@tasks.task
def update_comment_async(post_identifier, recursive=False):
    upsert_comment_chain(mongo, post_identifier, recursive)


@tasks.task
def batch_update_async(batch_items: dict):
    # todo break this batch into posts and account updates
    lag = time_delta(find_latest_item(mongo, 'Posts', 'created'))

    if lag > 1000:
        return

    for identifier in batch_items['comments']:
        with log_exceptions():
            upsert_comment_chain(mongo, identifier, recursive=True)

    # if we're lagging by a large margin, don't bother updating accounts
    if lag > 500:
        return

    for account_name in batch_items['accounts_light']:
        with log_exceptions():
            update_account(mongo, account_name, load_extras=False)
            # update_account_ops_quick(mongo, account_name) # TODO: re-enable this
    for account_name in batch_items['accounts']:
        with log_exceptions():
            update_account(mongo, account_name, load_extras=True)
            # update_account_ops_quick(mongo, account_name) # TODO: re-enable this
