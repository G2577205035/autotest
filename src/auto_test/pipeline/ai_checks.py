"""AI 功能接口连通性检查，各检查函数均可独立调用。"""

import base64
import json
import time
from pathlib import Path

import requests

from auto_test.common import runtime_config as config
from auto_test.common.logging import log


def _result(name, success, detail, started):
    return {
        "name": name,
        "success": bool(success),
        "detail": str(detail),
        "elapsed_s": round(time.time() - started, 1),
    }


def _auth_headers(satoken, content_type="application/json",
                  accept="application/json, text/plain, */*"):
    """构建沿用当前登录态的公共请求头。"""
    return {
        "Accept": accept,
        "Content-Type": content_type,
        "satoken": satoken,
        "Cookie": f"satoken={satoken}",
        "Origin": f"http://{config.HOST}",
        "Referer": f"http://{config.HOST}/",
    }


def _user_id_from_token(satoken):
    """从 Sa-Token JWT 的 loginId JSON 中提取当前用户 ID。"""
    try:
        payload = satoken.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        token_data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        login_id = token_data.get("loginId", {})
        if isinstance(login_id, str):
            login_id = json.loads(login_id)
        return str(login_id.get("id") or "")
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return ""


def get_model_info(satoken, timeout=30):
    """获取当前用户模型，返回 (检查结果, 模型信息)。"""
    started = time.time()
    name = "获取模型信息"
    log.info(f"AI功能检查 [{name}] 开始检查...")
    try:
        uid = _user_id_from_token(satoken)
        if not uid:
            return _result(name, False, "无法从当前 satoken 提取用户 ID", started), None
        response = requests.post(
            f"http://{config.HOST}/system/model-info/getModelInfo",
            data={"uid": uid},
            headers=_auth_headers(
                satoken,
                content_type="application/x-www-form-urlencoded;charset=UTF-8",
            ),
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        model = body.get("data") if body.get("code") == 200 else None
        if not isinstance(model, dict) or not model.get("modelName"):
            return _result(name, False, f"响应中无有效模型：{body.get('message', body)}", started), None
        detail = f"模型={model['modelName']}，maxToken={model.get('maxToken', '未知')}"
        return _result(name, True, detail, started), model
    except Exception as exc:
        return _result(name, False, f"{type(exc).__name__}: {exc}", started), None


def check_simple_translation(satoken, sample_path, timeout=180):
    """使用固定 35 种语言文本检查简易翻译接口。"""
    started = time.time()
    name = "简易翻译"
    log.info(f"AI功能检查 [{name}] 开始检查...")
    try:
        path = Path(sample_path)
        if not path.is_file():
            return _result(name, False, f"测试文本不存在：{path}", started)
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return _result(name, False, "35 种语言测试文本为空", started)
        response = requests.post(
            f"http://{config.HOST}/translate/translation_config_language/translateTextTwo",
            data={"content": content, "from": "es", "to": "zh-CHS"},
            headers=_auth_headers(
                satoken,
                content_type="application/x-www-form-urlencoded;charset=UTF-8",
            ),
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        translated = body.get("data")
        success = body.get("code") == 200 and bool(str(translated or "").strip())
        detail = f"HTTP {response.status_code}，返回内容 {len(str(translated or ''))} 字符"
        if not success:
            detail += f"，message={body.get('message', '')}"
        return _result(name, success, detail, started)
    except Exception as exc:
        return _result(name, False, f"{type(exc).__name__}: {exc}", started)


def _check_qanything_stream(name, endpoint, satoken, file_ids, model, timeout=300):
    """检查小逸助手 SSE 接口，采集足够的流式内容或读取到结束标记。"""
    started = time.time()
    log.info(f"AI功能检查 [{name}] 开始检查（选取文件 {len(file_ids)} 篇）...")
    try:
        if not file_ids:
            return _result(name, False, "没有可用于请求的文件 ID", started)
        if not model:
            return _result(name, False, "未获取到模型信息", started)
        max_token = int(model.get("maxToken") or 32768)
        response = requests.post(
            f"http://{config.HOST}{endpoint}",
            json={
                "fileIds": ",".join(str(fid) for fid in file_ids),
                "modelName": model["modelName"],
                "temperature": 0.8,
                "maxTokens": max(1, max_token - 52),
            },
            headers=_auth_headers(satoken, accept="text/event-stream"),
            stream=True,
            timeout=(30, timeout),
        )
        response.raise_for_status()
        event_count = 0
        content_parts = []
        done = False
        preview_limit = 120
        for raw_line in response.iter_lines(decode_unicode=True):
            line = raw_line or ""
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                done = True
                break
            event_count += 1
            try:
                event = json.loads(data)
                if isinstance(event, dict):
                    # reasoning 是模型思考过程；检查结果只展示最终正文 text。
                    effective_content = str(event.get("text") or "")
                else:
                    effective_content = str(event)
            except json.JSONDecodeError:
                effective_content = data
            if not effective_content.strip():
                continue
            content_parts.append(effective_content)
            if sum(len(part) for part in content_parts) >= preview_limit:
                response.close()
                break
        collected_content = "".join(content_parts)
        content_chars = len(collected_content)
        content_preview = collected_content.replace("\r\n", "\n").replace("\r", "\n").strip()
        error_markers = ("输入内容过长", "超过模型上下文上限")
        success = (
            event_count > 0
            and content_chars > 0
            and not any(marker in content_preview for marker in error_markers)
        )
        return _result(
            name, success,
            f"HTTP {response.status_code}，选取文件 {len(file_ids)} 篇，"
            f"SSE事件 {event_count} 条，结束标记={'是' if done else '否'}，"
            f"已采集内容 {content_chars} 字符，"
            f"正文预览：\n{content_preview[:preview_limit]}{'…' if len(content_preview) > preview_limit else ''}",
            started,
        )
    except Exception as exc:
        return _result(name, False, f"{type(exc).__name__}: {exc}", started)


def check_xiaoyi_translation(satoken, file_ids, model, timeout=300):
    return _check_qanything_stream(
        "小逸助手-AI翻译", "/qanything/ai/translate/stream",
        satoken, file_ids, model, timeout,
    )


def check_xiaoyi_summary(satoken, file_ids, model, timeout=300):
    return _check_qanything_stream(
        "小逸助手-AI总结", "/qanything/ai/summary/stream",
        satoken, file_ids, model, timeout,
    )


def run_ai_feature_checks(satoken, file_ids, cfg):
    """执行全部 AI 检查；每项独立失败，不中断其他检查。"""
    if not cfg.get("enable", True):
        return []
    results = []
    model = None
    needs_model = (
        cfg.get("xiaoyi_translation_enable", True)
        or cfg.get("xiaoyi_summary_enable", True)
    )
    if needs_model:
        model_result, model = get_model_info(satoken, cfg.get("request_timeout", 30))
        results.append(model_result)
        log.info(f"AI功能检查 [{model_result['name']}] {'通过' if model_result['success'] else '失败'}：{model_result['detail']}（耗时 {model_result['elapsed_s']}s）")
    max_ids = int(cfg.get("max_file_ids", 3))
    selected_ids = file_ids[:max_ids]
    checks = [
        (cfg.get("simple_translation_enable", True), lambda: check_simple_translation(
            satoken, cfg.get("sample_path", ""), cfg.get("stream_timeout", 300))),
        (cfg.get("xiaoyi_translation_enable", True), lambda: check_xiaoyi_translation(
            satoken, selected_ids, model, cfg.get("stream_timeout", 300))),
        (cfg.get("xiaoyi_summary_enable", True), lambda: check_xiaoyi_summary(
            satoken, selected_ids, model, cfg.get("stream_timeout", 300))),
    ]
    for enabled, check in checks:
        if not enabled:
            continue
        try:
            result = check()
        except Exception as exc:
            result = _result("未知AI功能", False, f"{type(exc).__name__}: {exc}", time.time())
        results.append(result)
        log.info(
            f"AI功能检查 [{result['name']}] "
            f"{'通过' if result['success'] else '失败'}：{result['detail']}"
        )
    return results
