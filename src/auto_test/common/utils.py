def safe_int(value, default=0):
    """安全转整数"""
    if value is None or value == "NOT_FOUND" or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def json_extract(data, jsonpath):
    """
    简易 JSON 提取器
    $..key  → 深度搜索第一个匹配
    $.data[*].key → 返回列表（兼容 data 是数组或 data.rows/data.list 是数组）
    """
    if jsonpath.startswith("$.."):
        key = jsonpath[3:]
        return _deep_search(data, key)
    if jsonpath == "$.data[*].id":
        d = data.get("data")
        if isinstance(d, list):
            return [item.get("id") for item in d if item.get("id")]
        if isinstance(d, dict):
            for list_key in ("rows", "list", "records"):
                inner = d.get(list_key)
                if isinstance(inner, list):
                    return [item.get("id") for item in inner if item.get("id")]
        return []
    return None


def _deep_search(obj, key):
    """深度搜索 JSON 中第一个匹配的 key"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = _deep_search(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _deep_search(item, key)
            if result is not None:
                return result
    return None
