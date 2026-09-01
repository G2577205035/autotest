from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.integrations.api import login as api_login, create_user as api_create_user


def login(username, password, host=None):
    """登录并返回 satoken，失败返回 None"""
    return api_login(host or config.HOST, username, password)


def login_or_fail(username, password):
    """登录，失败则抛异常"""
    satoken = login(username, password)
    if not satoken:
        raise RuntimeError(f"登录失败：{username}")
    return satoken


def create_user(admin_satoken, username, password):
    """用管理员 token 创建新用户"""
    return api_create_user(config.HOST, admin_satoken, username, password)
