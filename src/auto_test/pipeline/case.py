import time

from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.integrations.api import list_cases as api_list_cases, create_case as api_create_case


def resolve_case(satoken, case_name):
    """
    按名称查询案件：
    - 仅一个匹配 → 直接返回其 ID
    - 无匹配 → 以用户输入的名称创建新案件
    - 多个匹配（重名）→ 以 案件名_日期 创建新案件
    """
    cases = api_list_cases(config.HOST, satoken)
    matched = [c for c in cases if c.get("caseName") == case_name]

    if len(matched) == 1:
        case_id = matched[0]["id"]
        log.info(f"案件已存在：{case_name}（id={case_id}）")
        return case_id

    if len(matched) > 1:
        log.warning(f"案件名 '{case_name}' 存在 {len(matched)} 个重名，将以 案件名_日期 创建")
        case_name = f"{case_name}_{time.strftime('%Y%m%d')}"

    if not api_create_case(config.HOST, satoken, case_name):
        log.error(f"创建案件失败：{case_name}")
        return None

    cases = api_list_cases(config.HOST, satoken)
    for c in cases:
        if c.get("caseName") == case_name:
            log.info(f"使用案件：{case_name}（id={c['id']}）")
            return c["id"]

    log.error(f"案件已创建但查询不到：{case_name}")
    return None
