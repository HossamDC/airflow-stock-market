from airflow.decorators import dag, task
from airflow.hooks.base import BaseHook
from airflow.sensors.base import PokeReturnValue
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator
# from astro import sql as aql
# from astro.files import File
# from astro.sql.table import Table, Metadata

from airflow.providers.postgres.hooks.postgres import PostgresHook 
from datetime import datetime

from include.stock_market.tasks import _get_stock_prices, _store_prices, _get_formatted_csv , _stg_and_merge, BUCKET_NAME

SYMBOL = 'NVDA'

@dag(
    start_date=datetime(2023, 1, 1),
    schedule='@daily',
    catchup=False,
    tags=['stock_market'],

)
def stock_market():
    
    @task.sensor(poke_interval=30, timeout=300, mode='poke')
    def is_api_available() -> PokeReturnValue:
        import requests
        
        api = BaseHook.get_connection('stock_api')
        url = f"{api.host}{api.extra_dejson['endpoint']}"
        print(url)
        response = requests.get(url, headers=api.extra_dejson['headers'])
        condition = response.json()['finance']['result'] is None
        return PokeReturnValue(is_done=condition, xcom_value=url)
    
    get_stock_prices = PythonOperator(
        task_id='get_stock_prices',
        python_callable=_get_stock_prices,
        op_kwargs={'url': '{{ ti.xcom_pull(task_ids="is_api_available") }}', 'symbol': SYMBOL}
    )
    
    store_prices = PythonOperator(
        task_id='store_prices',
        python_callable=_store_prices,
        op_kwargs={'stock': '{{ ti.xcom_pull(task_ids="get_stock_prices") }}'}
    )
    
    format_prices = DockerOperator(
        task_id='format_prices',
        image='airflow/stock-app',
        container_name='format_prices',
        api_version='auto',
        auto_remove='success',
        docker_url='tcp://docker-proxy:2375',
        network_mode='container:spark-master',
        tty=True,
        xcom_all=False,
        mount_tmp_dir=False,
        environment={
            'SPARK_APPLICATION_ARGS': '{{ ti.xcom_pull(task_ids="store_prices") }}'
        }
    )
    

    get_formatted_csv = PythonOperator(
        task_id='get_formatted_csv',
        python_callable=_get_formatted_csv,
        op_kwargs={
            'path': '{{ ti.xcom_pull(task_ids="store_prices") }}'
        }
    )
    

    
    merge_to_postgres = PythonOperator(
        task_id='merge_to_postgres',                                # NEW
        python_callable=_stg_and_merge,                             # NEW
        op_kwargs={'path': '{{ ti.xcom_pull("store_prices") }}'}    # NEW
    )

    
    is_api_available() >> get_stock_prices >> store_prices >> format_prices >> get_formatted_csv >>  merge_to_postgres
        

stock_market()
    