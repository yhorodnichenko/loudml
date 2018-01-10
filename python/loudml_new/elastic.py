"""
Elasticsearch module for LoudML
"""

import datetime
import logging

import elasticsearch.exceptions
import urllib3.exceptions

import numpy as np

from elasticsearch import (
    Elasticsearch,
    helpers,
    TransportError,
)

from .datasource import DataSource
from . import (
    parse_addr,
)

def get_date_range(field, from_date=None, to_date=None):
    """
    Build date range for search query
    """

    date_range = {}

    if from_date is not None:
        date_range['gte'] = from_date
    if to_date is not None:
        date_range['lte'] = to_date

    if len(date_range) == 0:
        return None

    return {'range': {
        field: date_range,
    }}

class ElasticsearchDataSource(DataSource):
    """
    Elasticsearch datasource
    """

    def __init__(self, addr, timeout=30):
        super().__init__()
        self.addr = addr
        self._es = None
        self._timeout = 30

    @property
    def es(self):
        if self._es is None:
            addr = parse_addr(self.addr, default_port=9200)
            logging.info('connecting to elasticsearch on %s:%d',
                         addr['host'], addr['port'])
            self._es = Elasticsearch([addr], timeout=self._timeout)

        # urllib3 & elasticsearch modules log exceptions, even if they are
        # caught! Disable this.
        urllib_logger = logging.getLogger('urllib3')
        urllib_logger.setLevel(logging.CRITICAL)
        es_logger = logging.getLogger('elasticsearch')
        es_logger.setLevel(logging.CRITICAL)

        return self._es

    def create_index(self, index, template):
        """
        Create index and put template
        """

        self.es.indices.create(index=index)
        self.es.indices.put_template(name='%s-template' % index, body=template)

    def delete_index(self, name):
        """
        Delete index
        """
        self.es.indices.delete(index=name, ignore=404)

    def send_bulk(self, requests):
        """
        Send data to Elasticsearch
        """
        logging.info("commit %d change(s) to elasticsearch", len(requests))

        try:
            helpers.bulk(
                self.es,
                requests,
                chunk_size=5000,
                timeout="30s",
            )
        except (
            urllib3.exceptions.HTTPError,
            elasticsearch.exceptions.TransportError,
        ) as exn:
            raise errors.TransportError(str(exn))

    def insert_data(self, index, data, doc_type='generic', doc_id=None):
        """
        Insert entry into the index
        """

        req = {
            '_index': index,
            '_type': doc_type,
            '_source': data,
        }

        if doc_id is not None:
            req['_id'] = doc_id

        self.enqueue(req)

    def insert_times_data(self, ts, data, index):
        """
        Insert time-indexed entry
        """
        data['timestamp'] = int(ts * 1000)
        self.insert_data(index, data)

    def _get_es_agg(
            self,
            model,
            from_date_ms=None,
            to_date_ms=None,
        ):
        body = {
          "size": 0,
          "query": {
            "bool": {
              "must": [
              ],
              "should": [
              ],
              "minimum_should_match": 0,
            }
          },
          "aggs": {
            "histogram": {
              "date_histogram": {
                "field": "timestamp",
                "extended_bounds": {
                    "min": from_date_ms,
                    "max": to_date_ms,
                },
                "interval": "%ds" % model.bucket_interval,
                "min_doc_count": 0,
                "order": {
                  "_key": "asc"
                }
              },
              "aggs": {
              },
            }
          }
        }

        must = []
        must.append(get_date_range('timestamp', from_date_ms, to_date_ms))
        if len(must) > 0:
            body['query'] = {
                'bool': {
                    'must': must,
                }
            }

        aggs = {}
        for feature in model.features:
            if feature['metric'] == 'std_deviation' \
                or feature['metric'] == 'variance':
                sub_agg = 'extended_stats'
            else:
                sub_agg = 'stats'

            if 'script' in feature:
                agg = {
                    sub_agg: {
                        "script": {
                            "lang": "painless",
                            "inline": feature['script'],
                        }
                    }
                }
            elif 'field' in feature:
                agg = {
                    sub_agg: {
                        "field": feature['field'],
                    }
                }

            aggs[feature['name']] = agg

        for x in sorted(aggs):
            body['aggs']['histogram']['aggs'][x] = aggs[x]

        return body

    @staticmethod
    def _get_agg_val(bucket, feature):
        """
        Get aggregation value for the bucket returned by Elasticsearch
        """
        name = feature['name']
        metric = feature['metric']
        agg_val = bucket[name][metric]

        if agg_val is None:
            logging.info(
                "missing data: field '%s', metric: '%s', bucket: %s",
                feature['field'], metric, bucket['key'],
            )
            if feature.get('nan_is_zero', False):
                # Write zeros to encode missing data
                agg_val = 0
            else:
                # Use NaN to encode missing data
                agg_val = np.nan

        return agg_val

    def get_times_data(
        self,
        model,
        from_date=None,
        to_date=None,
    ):
        features = model.features
        nb_features = len(features)

        es_params={}
        if model.routing is not None:
            es_params['routing'] = model.routing

        body = self._get_es_agg(
            model,
            from_date_ms=int(from_date * 1000),
            to_date_ms=int(to_date * 1000),
        )

        try:
            es_res = self.es.search(
                index=model.index,
                size=0,
               body=body,
                params=es_params,
            )
        except (
            elasticsearch.exceptions.TransportError,
            urllib3.exceptions.HTTPError,
        ) as exn:
            logging.error("get_times_data: %s", str(exn))
            raise TransportError(str(exn))

        hits = es_res['hits']['total']
        if hits == 0:
            logging.info("Aggregations for model %s: Missing data", model.name)
            return

        # TODO: last bucket may contain incomplete data when to_date == now
        """
        now = datetime.datetime.now().timestamp()
        epoch_ms = 1000 * int(now)
        min_bound_ms = 1000 * int(now / model.bucket_interval) * model.bucket_interval
        """

        t0 = None

        for bucket in es_res['aggregations']['histogram']['buckets']:
            X = np.zeros(nb_features, dtype=float)
            timestamp = int(bucket['key'])
            timeval = bucket['key_as_string']

            i = 0
            for feature in features:
                X[i] = self._get_agg_val(bucket, feature)
                i += 1

            # TODO: last bucket may contain incomplete data when to_date == now
            """
            try:
                # The last interval contains partial data
                if timestamp == min_bound_ms:
                    R = float(epoch_ms - min_bound_ms) / (1000 * model.bucket_interval)
                    X = R * X + (1-R) * X_prev
            except NameError:
                # X_prev not defined. No interleaving required.
                pass

            X_prev = X
            """

            if t0 is None:
                t0 = timestamp

            yield (timestamp - t0) / 1000, X, timeval