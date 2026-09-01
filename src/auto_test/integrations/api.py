"""Compatibility functions backed by :class:`api_client.TargetPlatformClient`.

Existing modules keep their original function signatures while transport,
timeouts, retries, response validation, and connection reuse live in the
client implementation.
"""

from auto_test.integrations.api_client import (
    ApiPorts,
    TargetPlatformApiError,
    TargetPlatformClient,
    TargetPlatformConnectionError,
    TargetPlatformResponseError,
    client_for,
)


def login(host, username, password):
    return client_for(host).login(username, password)


def create_user(host, admin_satoken, username, password):
    return client_for(host).create_user(admin_satoken, username, password)


def check_upload_disk(host, satoken):
    return client_for(host).check_upload_disk(satoken)


def list_cases(host, satoken):
    return client_for(host).list_cases(satoken)


def create_case(host, satoken, case_name):
    return client_for(host).create_case(satoken, case_name)


def upload_files(host, satoken, case_id, case_name, data_name,
                 translate_name, analysis, file_list):
    return client_for(host).upload_files(
        satoken, case_id, case_name, data_name, translate_name, analysis, file_list
    )


def listen_multi_fast_upload(host, satoken, case_id, case_name, data_name,
                             file_path, translate_name, analysis):
    return client_for(host).listen_multi_fast_upload(
        satoken, case_id, case_name, data_name, file_path, translate_name, analysis
    )


def get_upload_status(host, satoken, ul_id):
    return client_for(host).get_upload_status(satoken, ul_id)


def list_upload_logs(host, satoken, data_name=""):
    return client_for(host).list_upload_logs(satoken, data_name=data_name)


def list_file_info(host, satoken, ul_id):
    return client_for(host).list_file_info(satoken, ul_id)


def get_file_detail(host, satoken, file_id):
    return client_for(host).get_file_detail(satoken, file_id)


def get_translate_content(host, satoken, file_id):
    return client_for(host).get_translate_content(satoken, file_id)


def export_translation(host, satoken, case_id, ul_ids=""):
    return client_for(host).export_translation(satoken, case_id, ul_ids)


def export_original(host, satoken, case_id, ul_ids=""):
    return client_for(host).export_original(satoken, case_id, ul_ids)


def list_download_tasks(host, satoken, page_size=100):
    return client_for(host).list_download_tasks(satoken, page_size=page_size)


__all__ = [
    "ApiPorts", "TargetPlatformApiError", "TargetPlatformClient",
    "TargetPlatformConnectionError", "TargetPlatformResponseError",
    "check_upload_disk", "create_case", "create_user", "export_original",
    "export_translation", "get_file_detail", "get_translate_content",
    "get_upload_status", "list_cases", "list_download_tasks", "list_file_info",
    "list_upload_logs", "listen_multi_fast_upload", "login", "upload_files",
]
