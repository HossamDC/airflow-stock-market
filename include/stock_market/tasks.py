from airflow.hooks.base import BaseHook
import json
from minio import Minio
from io import BytesIO
from airflow.exceptions import AirflowNotFoundException
import os                                               # NEW
import csv                                              # NEW
import tempfile                                         # NEW
from airflow.providers.postgres.hooks.postgres import PostgresHook  # NEW

BUCKET_NAME = 'stock-market'

def _get_minio_client():
    minio = BaseHook.get_connection('minio')
    client = Minio(
        endpoint=minio.extra_dejson['endpoint_url'].split('//')[1],
        access_key=minio.login,
        secret_key=minio.password,
        secure=False
    )
    return client

def _get_stock_prices(url, symbol):
    import requests
    
    url = f"{url}{symbol}?metrics=high?&interval=1d&range=1y"
    api = BaseHook.get_connection('stock_api')
    response = requests.get(url, headers=api.extra_dejson['headers'])
    return json.dumps(response.json()['chart']['result'][0])

def _store_prices(stock):
    client = _get_minio_client()
    
    if not client.bucket_exists(BUCKET_NAME):
        client.make_bucket(BUCKET_NAME)
    stock = json.loads(stock)
    symbol = stock['meta']['symbol']
    data = json.dumps(stock, ensure_ascii=False).encode('utf8')
    objw = client.put_object(
        bucket_name=BUCKET_NAME,
        object_name=f'{symbol}/prices.json',
        data=BytesIO(data),
        length=len(data)
    )
    return f'{objw.bucket_name}/{symbol}'
    
def _get_formatted_csv(path):
    client = _get_minio_client()
    prefix_name = f"{path.split('/')[1]}/formatted_prices/"
    objects = client.list_objects(BUCKET_NAME, prefix=prefix_name, recursive=True)
    for obj in objects:
        if obj.object_name.endswith('.csv'):
            return obj.object_name
    return AirflowNotFoundException('The csv file does not exist')




def _stg_and_merge(path):
     """
     NEW: يحمل جميع ملفات CSV من MinIO (تحت prefix formatted_prices/),
     يحمّلها دفعة واحدة إلى جدول staging ثم يدمج DELTA في الجدول النهائي.
     """
     client = _get_minio_client()
     prefix = f"{path.split('/')[1]}/formatted_prices/"
     # نجمع كل أسماء ملفات CSV
     objects = client.list_objects(BUCKET_NAME, prefix=prefix, recursive=True)
     csv_objects = [obj.object_name for obj in objects if obj.object_name.endswith('.csv')]
     if not csv_objects:
         raise FileNotFoundError(f"No CSVs under {prefix}")
 
     # أنشئ جدول staging في Postgres
     pg = PostgresHook(postgres_conn_id='postgres')
     conn = pg.get_conn()
     cur = conn.cursor()
     cur.execute("DROP TABLE IF EXISTS public.stock_market_stg;")
     conn.commit()
     cur.execute("""
    CREATE TABLE IF NOT EXISTS public.stock_market_stg (
        timestamp BIGINT,           
        date      DATE,
        close     NUMERIC,
        high      NUMERIC,
        low       NUMERIC,
        open      NUMERIC,
        volume    BIGINT,
        PRIMARY KEY (date)
    );
    TRUNCATE public.stock_market_stg;
    """)
     conn.commit()
 
     # حمّل كل CSV دفعة إلى staging
     for obj_name in csv_objects:
         tmpf = tempfile.NamedTemporaryFile(prefix='stg_', suffix='.csv', delete=False)
         client.fget_object(BUCKET_NAME, obj_name, tmpf.name)
         tmpf.close()
         with open(tmpf.name, 'r') as f:
             # عدّل delimiter حسب CSV (مثال: tab أو comma)
             cur.copy_expert(
                 sql="COPY public.stock_market_stg (timestamp,close,high,low,open,volume,date) "
                     "FROM STDIN WITH CSV HEADER DELIMITER ',';",
                 file=f
             )
         os.remove(tmpf.name)
     conn.commit()
 
     # ادمج DELTA من staging إلى final
     cur.execute("""
         CREATE TABLE IF NOT EXISTS public.stock_market (
             date    DATE      PRIMARY KEY,
             close   NUMERIC,
             high    NUMERIC,
             low     NUMERIC,
             open    NUMERIC,
             volume  BIGINT
         );
         INSERT INTO public.stock_market (date, close, high, low, open, volume)
         SELECT s.date, s.close, s.high, s.low, s.open, s.volume
         FROM public.stock_market_stg s
         WHERE NOT EXISTS (
             SELECT 1 FROM public.stock_market t WHERE t.date = s.date
         );
     """)
     conn.commit()
     cur.close()
     conn.close()
     return f"Merged {len(csv_objects)} file(s) into stock_market." 
    