import argparse
import datetime
import time
import base64
import logging
import json
import signal
import os
import sys

import multiprocessing
import multiprocessing.pool
from multiprocessing import TimeoutError 

from .times import async_times_train_model
from .times import async_times_range_predict
from .times import async_times_live_predict

from .ivoip import async_ivoip_train_model
from .ivoip import async_ivoip_map_account
from .ivoip import async_ivoip_map_accounts
from .ivoip import async_ivoip_score_hist
from .ivoip import async_ivoip_live_predict

get_current_time = lambda: int(round(time.time()))

from flask import (
    Flask,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
)

from .storage import (
    HTTPError,
    StorageException,
    Storage,
)

class NoDaemonProcess(multiprocessing.Process):
    # make 'daemon' attribute always return False
    def _get_daemon(self):
        return False
    def _set_daemon(self, value):
        pass
    daemon = property(_get_daemon, _set_daemon)

# We sub-class multiprocessing.pool.Pool instead of multiprocessing.Pool
# because the latter is only a wrapper function, not a proper class.
class Pool(multiprocessing.pool.Pool):
    Process = NoDaemonProcess


import threading
from threading import current_thread

g_elasticsearch_addr = None
g_job_id = 0
g_processes = {}
g_jobs = {}
g_pool = None
threadLocal = threading.local()

app = Flask(__name__, static_url_path='/static', template_folder='templates')

def get_storage():
    global g_elasticsearch_addr
    storage = getattr(threadLocal, 'storage', None)
    if storage is None:
        storage = Storage(g_elasticsearch_addr)
        threadLocal.storage = storage

    return storage

def log_message(format, *args):
    if len(request.remote_addr) > 0:
        addr = request.remote_addr
    else:
        addr = "-"

    sys.stdout.write("%s - - [%s] %s\n" % (addr,
                     # log_date_time_string()
                     "-", format % args))

def log_error(format, *args):
    log_message(format, *args)

@app.errorhandler(HTTPError)
def exn_handler(exn):
    response = jsonify({
        'error': "Internal",
    })
    response.status_code = 500
    return response

def error_msg(msg, code):
    response = jsonify({
        'error': msg,
    })
    response.status_code = code
    return response


def terminate_job(job_id, timeout):
    global g_jobs
    try:
        res = g_jobs[job_id].wait(timeout)
    except(TimeoutError):
        g_jobs[job_id].terminate()
        pass

    g_jobs.pop(job_id, None)
    return 

def start_training_job(name, from_date, to_date, train_test_split):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, from_date, to_date)
    g_jobs[g_job_id] = g_pool.apply_async(async_times_train_model, args)

    return g_job_id

def start_inference_job(name, from_date, to_date):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, from_date, to_date)
    g_jobs[g_job_id] = g_pool.apply_async(async_times_range_predict, args)

    return g_job_id

def run_ivoip_training_job(name,
                           from_date,
                           to_date,
                           num_epochs=100,
                           limit=-1,
                           ):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, from_date, to_date, num_epochs, limit)
    g_jobs[g_job_id] = g_pool.apply_async(async_ivoip_train_model, args)

    return g_job_id

def run_async_map_account(name,
                           from_date,
                           to_date,
                           account_name):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, account_name, from_date, to_date)
    g_jobs[g_job_id] = g_pool.apply_async(async_ivoip_map_account, args)

    return g_job_id

def run_async_map_accounts(name,
                           from_date,
                           to_date):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, from_date, to_date)
    g_jobs[g_job_id] = g_pool.apply_async(async_ivoip_map_accounts, args)

    return g_job_id

def run_async_score_hist(name,
                         from_date,
                         to_date,
                         span,
                         interval,
    ):
    global g_elasticsearch_addr
    global g_pool
    global g_job_id
    global g_jobs

    g_job_id = g_job_id + 1
    args = (g_elasticsearch_addr, name, from_date, to_date, span, interval)
    g_jobs[g_job_id] = g_pool.apply_async(async_ivoip_score_hist, args)

    return g_job_id

def get_job_status(job_id, timeout=1):
    global g_jobs
    res = g_jobs[job_id]
    try:
        successful = res.successful()
    except (AssertionError):
        successful = None

    try:
        result = res.get(timeout)
    except(TimeoutError):
        result = None

    return {
        'ready': res.ready(),
        'successful': successful,
        'result': result, 
    }

def start_predict_job(name):
    global g_processes
    global g_elasticsearch_addr

    args = (g_elasticsearch_addr, name)
    p = multiprocessing.Process(target=async_times_live_predict, args=args)
    p.start()
    g_processes[name] = p
    return 

def stop_predict_job(name):
    global g_processes
    p = g_processes[name]
    if p is not None:
        del g_processes[name]
        os.kill(p.pid, signal.SIGUSR1)
        os.waitpid(p.pid, 0)
        return 

def start_ivoip_job(name):
    global g_processes
    global g_elasticsearch_addr

    args = (g_elasticsearch_addr, name)
    p = multiprocessing.Process(target=async_ivoip_live_predict, args=args)
    p.start()
    g_processes[name] = p
    return 

def stop_ivoip_job(name):
    return stop_predict_job(name)

@app.route('/api/core/set_threshold', methods=['POST'])
def set_threshold():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    # anomaly threshold between 0 and 100
    threshold = int(request.args.get('threshold', 30))

    if name is None:
        return error_msg(msg='Bad Request', code=400)

    storage.set_threshold(
        name=name,
        threshold=threshold)
    return error_msg(msg='', code=200)

@app.route('/api/times/create', methods=['POST'])
def create_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    # The index name to query at periodic interval 
    index = request.args.get('index', None)
    # ES _routing information to query the index 
    routing = request.args.get('routing', None)
    # time offset, in seconds, when querying the index 
    offset = int(request.args.get('offset', 30))
    # time span, in seconds, to aggregate features
    span = int(request.args.get('span', 300))
    # bucket time span, in seconds, to aggregate features
    bucket_interval = int(request.args.get('bucket_interval', 60))
    # periodic interval to run queries
    interval = int(request.args.get('interval', 60))
    if name is None or index is None:
        return error_msg(msg='Bad Request', code=400)

    # features { .name, .field, .script, .metric }
    data = request.get_json()
    features = data['features']
    if features is None or len(features) == 0:
        return error_msg(msg='Bad Request', code=400)

    storage.create_model(
        name=name,
        index=index,
        routing=routing,
        offset=offset,
        span=span,
        bucket_interval=bucket_interval,
        interval=interval,
        features=features,
    )
  
    return error_msg(msg='', code=200)

@app.route('/api/times/delete', methods=['POST'])
def delete_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    storage.delete_model(
        name=name,
    )
    return error_msg(msg='', code=200)

# Custom API to create quadrant data based on SUNSHINE paper.
@app.route('/api/ivoip/create', methods=['POST'])
def ivoip_create():
    global arg
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    # The index name to query at periodic interval 
    index = request.args.get('index', None)
    # The term used to aggregate profile data
    term = request.args.get('term', None)
    # The # terms in profile data
    max_terms = int(request.args.get('max_terms', 1000))
    # ES _routing information to query the index 
    routing = request.args.get('routing', None)
    # time offset, in seconds, when querying the index 
    offset = int(request.args.get('offset', 30))
    map_w = int(request.args.get('map_w', 50))
    map_h = int(request.args.get('map_h', 50))

    # time span in periodic queries
    span = int(request.args.get('span', 7*24*3600))
    # periodic interval to run queries
    interval = int(request.args.get('interval', 60))
    if name is None or index is None or term is None:
        return error_msg(msg='Bad Request', code=400)

    storage.create_ivoip(
        name=name,
        index=index,
        routing=routing,
        offset=offset,
        interval=interval,
        span=span,
        term=term,
        max_terms=max_terms,
        map_w=map_w,
        map_h=map_h,
    )
  
    return error_msg(msg='', code=200)

@app.route('/api/ivoip/delete', methods=['POST'])
def ivoip_delete():
    return delete_model()

@app.route('/api/ivoip/get_job_status', methods=['GET'])
def ivoip_job_status():
    return job_status()

@app.route('/api/ivoip/train', methods=['POST'])
def __ivoip_train_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    from_date = int(request.args.get('from_date', (get_current_time()-30*24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))
    num_epochs = int(request.args.get('epochs', 100))
    limit = int(request.args.get('limit', -1))

    job_id = run_ivoip_training_job(name,
                                    from_date=from_date,
                                    to_date=to_date,
                                    num_epochs=num_epochs,
                                    limit=limit,
                                    )

    return jsonify({'job_id': job_id})

@app.route('/api/ivoip/map', methods=['POST'])
def ivoip_map():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    account_name = request.args.get('account')
    if account_name is None:
        return error_msg(msg='Bad Request', code=400)

    # By default: calculate the short term (7 days) signature
    from_date = int(request.args.get('from_date', (get_current_time()-7*24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))

    job_id = run_async_map_account(name,
                                   from_date=from_date,
                                   to_date=to_date,
                                   account_name=account_name,
                                  )

    return jsonify({'job_id': job_id})

@app.route('/api/ivoip/map_x', methods=['POST'])
def ivoip_map_x():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    # By default: calculate the short term (7 days) signature
    from_date = int(request.args.get('from_date', (get_current_time()-7*24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))

    job_id = run_async_map_accounts(name,
                                   from_date=from_date,
                                   to_date=to_date,
                                  )

    return jsonify({'job_id': job_id})

@app.route('/api/ivoip/score_hist', methods=['POST'])
def ivoip_score_hist():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    # By default: trend the scoring histogram over the last 30 days
    from_date = int(request.args.get('from_date', (get_current_time()-30*24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))
    span = int(request.args.get('span', 7*24*3600))
    interval = int(request.args.get('interval', 7*24*3600))

    job_id = run_async_score_hist(name,
                                   from_date=from_date,
                                   to_date=to_date,
                                   span=span,
                                   interval=interval,
                                  )

    return jsonify({'job_id': job_id})

    
@app.route('/api/ivoip/start', methods=['POST'])
def ivoip_start_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    res = storage.find_model(
        name,
    )
    if res == True:
        start_ivoip_job(name)
        return error_msg(msg='', code=200)
    else:
        return error_msg(msg='Not found', code=404)

@app.route('/api/ivoip/stop', methods=['POST'])
def ivoip_stop_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    res = storage.find_model(
        name,
    )
    if res == True:
        stop_ivoip_job(name)
        return error_msg(msg='', code=200)
    else:
        return error_msg(msg='Not found', code=404)

@app.route('/api/core/get_job_status', methods=['GET'])
def job_status():
    job_id = request.args.get('job_id', None)
    if job_id is None:
        return error_msg(msg='Bad Request', code=400)

    res = get_job_status(int(job_id))
    return make_response(json.dumps(res))

@app.route('/api/times/train', methods=['POST'])
def train_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    from_date = int(request.args.get('from_date', (get_current_time()-24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))
    train_test_split = float(request.args.get('train_test_split', 0.67))

    job_id = start_training_job(name, from_date, to_date, train_test_split)

    return jsonify({'job_id': job_id})

@app.route('/api/times/inference', methods=['POST'])
def timeseries_inference():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    from_date = int(request.args.get('from_date', (get_current_time()-24*3600)))
    to_date = int(request.args.get('to_date', get_current_time()))

    job_id = start_inference_job(name, from_date, to_date)

    return jsonify({'job_id': job_id})

    
@app.route('/api/times/start', methods=['POST'])
def start_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    res = storage.find_model(
        name,
    )
    if res == True:
        start_predict_job(name)
        return error_msg(msg='', code=200)
    else:
        return error_msg(msg='Not found', code=404)

@app.route('/api/times/stop', methods=['POST'])
def stop_model():
    storage = get_storage()
    # The model name 
    name = request.args.get('name', None)
    if name is None:
        return error_msg(msg='Bad Request', code=400)

    res = storage.find_model(
        name,
    )
    if res == True:
        stop_predict_job(name)
        return error_msg(msg='', code=200)
    else:
        return error_msg(msg='Not found', code=404)

@app.route('/api/core/list', methods=['GET'])
def list_models():
    storage = get_storage()
    size = int(request.args.get('size', 10))

    return jsonify(storage.get_model_list(
            size=size,
        ))

def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def main():
    global g_elasticsearch_addr
    global g_pool
    global arg
    parser = argparse.ArgumentParser(
        description=main.__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        'elasticsearch_addr',
        help="Elasticsearch address",
        type=str,
        nargs='?',
        default="localhost:9200",
    )
    parser.add_argument(
        '-l', '--listen',
        help="Listen address",
        type=str,
        default="0.0.0.0:8077",
    )
    parser.add_argument(
        '--maxtasksperchild',
        help="Maxtasksperchild in process pool size",
        type=int,
        default=10,
    )
    parser.add_argument(
        '--autostart',
        help="Autostart inference jobs",
        type=str2bool,
        nargs='?',
        const=True,
        default=False,
    )
    parser.add_argument(
        '-w', '--workers',
        help="Worker processes pool size",
        type=int,
        default=multiprocessing.cpu_count(),
    )

    arg = parser.parse_args()

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    app.logger.setLevel(logging.INFO)

    g_elasticsearch_addr = arg.elasticsearch_addr

    storage = get_storage()
    try:
        es_res = storage.get_model_list()
        for doc in es_res:
            try:
                if 'term' in doc and (arg.autostart == True):
                    start_ivoip_job(doc['name'])
                elif (arg.autostart == True):
                    start_predict_job(doc['name'])
            except(Exception):
                pass

    except(StorageException):
        pass
   
    g_pool = Pool(processes=arg.workers, maxtasksperchild=arg.maxtasksperchild)
 
    host, port = arg.listen.split(':')
    app.run(host=host, port=port)

    g_pool.close()
    g_pool.join()

if __name__ == "__main__":
    # execute only if run as a script
    main()
