# pylint: disable=unsubscriptable-object
"""Holds the BigQuery ETL class"""
import json
import logging
import os
import pathlib
import re
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, cast

import google.auth
import google.cloud.bigquery as bq
import jinja2 as jj
from google.cloud.bigquery.schema import SchemaField
from jinja2.utils import select_autoescape

from ..etl import Etl
from .gcp import Gcp


class BigQuery(Etl):
    """
    ETL class that automates the extract-transfer-load process from source data to the OMOP common data model.
    """

    def __init__(
        self,
        credentials_file: Optional[str],
        project_id: Optional[str],
        location: Optional[str],
        dataset_id_raw: str,
        dataset_id_work: str,
        dataset_id_omop: str,
        bucket_uri: str,
        **kwargs,
    ):
        """Constructor

        Args:
            credentials_file (str): The credentials file must be a service account key, stored authorized user credentials or external account credentials.
            project_id (str): Project ID in GCP
            location (str): The location in GCP (see https://cloud.google.com/about/locations/)
            dataset_id_raw (str): Big Query dataset ID that holds the raw tables
            dataset_id_work (str): Big Query dataset ID that holds the work tables
            dataset_id_omop (str): Big Query dataset ID that holds the omop tables
            bucket_uri (str): The name of the Cloud Storage bucket and the path in the bucket (directory) to store the Parquet file(s) (the uri has format 'gs://{bucket_name}/{bucket_path}'). These parquet files will be the converted and uploaded 'custom concept' CSV's and the Usagi CSV's.
        ```
        """  # noqa: E501 # pylint: disable=line-too-long
        super().__init__(**kwargs)

        if credentials_file:
            credentials, project = google.auth.load_credentials_from_file(
                credentials_file
            )
        else:
            credentials, project = google.auth.default()

        if not project_id:
            project_id = project

        self._gcp = Gcp(credentials=credentials, location=location or "EU")
        self._project_id = cast(str, project_id)
        self._dataset_id_raw = dataset_id_raw
        self._dataset_id_work = dataset_id_work
        self._dataset_id_omop = dataset_id_omop
        self._bucket_uri = bucket_uri

        template_dir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "templates"
        )
        template_loader = jj.FileSystemLoader(searchpath=template_dir)
        self._template_env = jj.Environment(
            autoescape=select_autoescape(["sql"]), loader=template_loader
        )

    def create_omop_db(self) -> None:
        with open(
            os.path.join(
                pathlib.Path(__file__).parent.absolute(),
                "templates/OMOPCDM_bigquery_5.4_ddl.sql",
            ),
            encoding="UTF8",
        ) as file:
            ddl = file.read()

        ddl = re.sub(
            r"(?:create table @cdmDatabaseSchema)(\S*)",
            rf"create table if not exists `{self._project_id}.{self._dataset_id_omop}\1`",
            ddl,
        )
        ddl = re.sub(r".(?<!not )null", r"", ddl)
        ddl = re.sub(r"\"", r"", ddl)
        self._gcp.run_query_job(ddl)

        with open(
            os.path.join(
                pathlib.Path(__file__).parent.absolute(),
                "templates/OMOPCDM_bigquery_5.4_clustering_fields.json",
            ),
            encoding="UTF8",
        ) as file:
            clustering_fields = json.load(file)

        for table, fields in clustering_fields.items():
            self._gcp.set_clustering_fields_on_table(
                self._project_id, self._dataset_id_omop, table, fields
            )

    def _source_to_concept_map_update_invalid_reason(self, etl_start: date) -> None:
        template = self._template_env.get_template(
            "SOURCE_TO_CONCEPT_MAP_update_invalid_reason.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
        )
        self._gcp.run_query_job(
            sql,
            query_parameters=[bq.ScalarQueryParameter("etl_start", "DATE", etl_start)],
        )

    def _get_column_names(self, omop_table_name: str) -> List[str]:
        columns = self._gcp.get_column_names(
            self._project_id, self._dataset_id_omop, omop_table_name
        )
        return columns

    def _is_pk_auto_numbering(
        self, omop_table_name: str, omop_table_props: Any
    ) -> bool:
        # get the primary key meta data from the the destination OMOP table
        pk_column_metadata = self._gcp.get_column_metadata(
            self._project_id,
            self._dataset_id_omop,
            omop_table_name,
            omop_table_props.pk,
        )
        # is the primary key an auto numbering column?
        pk_auto_numbering = pk_column_metadata.get("data_type") == "INT64"
        return pk_auto_numbering

    def _clear_custom_concept_upload_table(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        self._gcp.delete_table(
            self._project_id,
            self._dataset_id_work,
            f"{omop_table}__{concept_id_column}_concept",
        )

    def _create_custom_concept_id_swap_table(self) -> None:
        template = self._template_env.get_template("CONCEPT_ID_swap_create.sql.jinja")
        ddl = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
        )
        self._gcp.run_query_job(ddl)

    def _load_custom_concepts_parquet_in_upload_table(
        self, parquet_file: str, omop_table: str, concept_id_column: str
    ) -> None:
        # upload the Parquet file to the Cloud Storage Bucket
        uri = self._gcp.upload_file_to_bucket(parquet_file, self._bucket_uri)
        # load the uploaded Parquet file from the bucket into the specific custom concept table in the work dataset
        self._gcp.batch_load_from_bucket_into_bigquery_table(
            uri,
            self._project_id,
            self._dataset_id_work,
            f"{omop_table}__{concept_id_column}_concept",
        )

    def _give_custom_concepts_an_unique_id_above_2bilj(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template("CONCEPT_ID_swap_merge.sql.jinja")
        sql = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
        )
        self._gcp.run_query_job(sql)

    def _merge_custom_concepts_with_the_omop_concepts(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template("CONCEPT_merge.sql.jinja")
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(sql)

    def _clear_usagi_load_table(self, omop_table: str, concept_id_column: str) -> None:
        self._gcp.delete_table(
            self._project_id,
            self._dataset_id_work,
            f"{omop_table}__{concept_id_column}_usagi",
        )

    def _create_usagi_load_table(self, omop_table: str, concept_id_column: str) -> None:
        template = self._template_env.get_template(
            "{omop_table}__{concept_id_column}_usagi_create.sql.jinja"
        )
        ddl = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(ddl)

    def _load_usagi_parquet_in_upload_table(
        self, parquet_file: str, omop_table: str, concept_id_column: str
    ) -> None:
        # upload the Parquet file to the Cloud Storage Bucket
        uri = self._gcp.upload_file_to_bucket(parquet_file, self._bucket_uri)
        # load the uploaded Parquet file from the bucket into the specific usagi table in the work dataset
        self._gcp.batch_load_from_bucket_into_bigquery_table(
            uri,
            self._project_id,
            self._dataset_id_work,
            f"{omop_table}__{concept_id_column}_usagi",
        )

    def _swap_usagi_source_value_for_concept_id(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "{omop_table}__{concept_id_column}_usagi_merge.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
            dataset_id_omop=self._dataset_id_omop,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
        )
        self._gcp.run_query_job(sql)

    def _store_usagi_source_value_to_concept_id_mapping(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "SOURCE_TO_CONCEPT_MAP_merge.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
            dataset_id_omop=self._dataset_id_omop,
        )
        self._gcp.run_query_job(sql)

    def _get_query_from_sql_file(self, sql_file: str, omop_table: str) -> str:
        with open(sql_file, encoding="UTF8") as file:
            select_query = file.read()
            if Path(sql_file).suffix == ".jinja":
                template = self._template_env.from_string(select_query)
                select_query = template.render(
                    project_id=self._project_id,
                    dataset_id_raw=self._dataset_id_raw,
                    dataset_id_work=self._dataset_id_work,
                    dataset_id_omop=self._dataset_id_omop,
                    omop_table=omop_table,
                )
        return select_query

    def _query_into_work_table(self, work_table: str, select_query: str) -> None:
        template = self._template_env.get_template(
            "{omop_table}_{sql_file}_insert.sql.jinja"
        )
        ddl = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            work_table=work_table,
            select_query=select_query,
        )
        self._gcp.run_query_job(ddl)

    def _create_pk_auto_numbering_swap_table(
        self, primary_key_column: str, concept_id_columns: List[str]
    ) -> None:
        template = self._template_env.get_template(
            "{primary_key_column}_swap_create.sql.jinja"
        )
        ddl = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            primary_key_column=primary_key_column,
            # foreign_key_columns=foreign_key_columns.__dict__,
            concept_id_columns=concept_id_columns,
        )
        self._gcp.run_query_job(ddl)

    def _execute_pk_auto_numbering_swap_query(
        self,
        omop_table: str,
        work_table: str,
        columns: List[str],
        primary_key_column: str,
        pk_auto_numbering: bool,
        foreign_key_columns: Any,
        concept_id_columns: List[str],
    ) -> None:
        template = self._template_env.get_template(
            "{primary_key_column}_swap_merge.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_work=self._dataset_id_work,
            columns=columns,
            primary_key_column=primary_key_column,
            foreign_key_columns=foreign_key_columns.__dict__,
            concept_id_columns=concept_id_columns,
            pk_auto_numbering=pk_auto_numbering,
            omop_table=omop_table,
            work_table=work_table,
        )
        self._gcp.run_query_job(sql)

    def _merge_into_omop_table(
        self,
        sql_file: str,
        omop_table: str,
        columns: List[str],
        primary_key_column: str,
        pk_auto_numbering: bool,
        foreign_key_columns: Any,
        concept_id_columns: List[str],
    ):
        logging.info("Merging query '%s' into omop table '%s'", sql_file, omop_table)
        template = self._template_env.get_template("{omop_table}_merge.sql.jinja")
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            omop_table=omop_table,
            dataset_id_work=self._dataset_id_work,
            sql_file=Path(Path(sql_file).stem).stem,
            columns=columns,
            primary_key_column=primary_key_column,
            foreign_key_columns=foreign_key_columns.__dict__,
            concept_id_columns=concept_id_columns,
            pk_auto_numbering=pk_auto_numbering,
        )
        self._gcp.run_query_job(sql)

    def _get_work_tables(self) -> List[str]:
        work_tables = self._gcp.get_table_names(self._project_id, self._dataset_id_work)
        return work_tables

    def _truncate_omop_table(self, table_name: str) -> None:
        template = self._template_env.get_template("cleanup/truncate.sql.jinja")
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            table_name=table_name,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_table(self) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_remove_custom_concepts.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_relationship_table(self) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_RELATIONSHIP_remove_custom_concepts.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_ancestor_table(self) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_ANCESTOR_remove_custom_concepts.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_table_using_usagi_table(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_remove_custom_concepts_by_{omop_table}__{concept_id_column}_concept_table.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_relationship_table_using_usagi_table(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_RELATIONSHIP_remove_custom_concepts_by_{omop_table}__{concept_id_column}_usagi_table.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(sql)

    def _remove_custom_concepts_from_concept_ancestor_table_using_usagi_table(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "cleanup/CONCEPT_ANCESTOR_remove_custom_concepts_by_{omop_table}__{concept_id_column}_usagi_table.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(sql)

    def _remove_source_to_concept_map_using_usagi_table(
        self, omop_table: str, concept_id_column: str
    ) -> None:
        template = self._template_env.get_template(
            "cleanup/SOURCE_TO_CONCEPT_MAP_remove_concepts_by_{omop_table}__{concept_id_column}_usagi_table.sql.jinja"
        )
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            min_custom_concept_id=Etl._CUSTOM_CONCEPT_IDS_START,
            omop_table=omop_table,
            concept_id_column=concept_id_column,
        )
        self._gcp.run_query_job(sql)

    def _delete_work_table(self, work_table: str) -> None:
        table_id = f"{self._project_id}.{self._dataset_id_work}.{work_table}"
        logging.info("Deleting table '%s'", table_id)
        self._gcp.delete_table(self._project_id, self._dataset_id_work, work_table)

    def _load_vocabulary_parquet_in_upload_table(
        self, parquet_file: str, vocabulary_table: str
    ) -> None:
        # match vocabulary_table:
        #     case "concept":
        #         schema = [
        #             SchemaField("concept_id", "INTEGER", mode="REQUIRED"),
        #             SchemaField("concept_name", "STRING", mode="REQUIRED"),
        #             SchemaField("domain_id", "STRING", mode="REQUIRED"),
        #             SchemaField("vocabulary_id", "STRING", mode="REQUIRED"),
        #             SchemaField("concept_class_id", "STRING", mode="REQUIRED"),
        #             SchemaField("standard_concept", "STRING", mode="NULLABLE"),
        #             SchemaField("concept_code", "STRING", mode="REQUIRED"),
        #             SchemaField("valid_start_date", "DATE", mode="REQUIRED"),
        #             SchemaField("valid_end_date", "DATE", mode="REQUIRED"),
        #             SchemaField("invalid_reason", "STRING", mode="NULLABLE"),
        #         ]

        # upload the Parquet file to the Cloud Storage Bucket
        uri = self._gcp.upload_file_to_bucket(parquet_file, self._bucket_uri)
        # load the uploaded Parquet file from the bucket into the specific custom concept table in the work dataset
        self._gcp.batch_load_from_bucket_into_bigquery_table(
            uri,
            self._project_id,
            self._dataset_id_work,
            vocabulary_table,
            write_disposition=bq.WriteDisposition.WRITE_EMPTY,  # , schema
        )

    def _clear_vocabulary_upload_table(self, vocabulary_table: str) -> None:
        self._gcp.delete_table(
            self._project_id, self._dataset_id_work, vocabulary_table
        )

    def _merge_uploaded_vocabulary_table(self, vocabulary_table: str) -> None:
        template = self._template_env.get_template("vocabulary_table_merge.sql.jinja")
        sql = template.render(
            project_id=self._project_id,
            dataset_id_omop=self._dataset_id_omop,
            dataset_id_work=self._dataset_id_work,
            vocabulary_table=vocabulary_table,
        )
        self._gcp.run_query_job(sql)
        # job = client.copy_table(source_table_id, destination_table_id)
        # job.result()
