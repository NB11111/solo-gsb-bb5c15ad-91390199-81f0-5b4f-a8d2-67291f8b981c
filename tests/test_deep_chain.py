"""深链（深度超过 Python 默认递归上限）的溯源链枚举回归测试。

修复要点：候选链枚举由递归 DFS 改为**显式栈**遍历——单条深链
（有效父证书逐级相连、深度超过 ``sys.getrecursionlimit()``）不再抛出
RecursionError 导致接口 500；起点与父边的确定性顺序、成环/失效父证书/
死路的终止记录以及 500 条路径截断（undetermined）语义全部保持不变。
"""
import sys

from fastapi.testclient import TestClient

AT = "2026-06-15T00:00:00"
AT_Z = AT + "Z"

# 超过 Python 默认递归上限（1000）的链深；旧递归实现在此深度必抛 RecursionError
DEPTH = sys.getrecursionlimit() + 200


def _deep_chain_payload(depth=DEPTH, broken_link=None):
    """构造 CW -> C00000 -> ... -> C{depth-1} -> CG 的单条深链提交载荷。

    不确定度沿链严格逐级放大（避免 UNCERTAINTY_NOT_DECAY 警告干扰断言）。
    ``broken_link`` 为 ("dead_end" | "cycle" | "inactive_parent", 深度) 时
    在指定深度处改造链路：断掉末端边 / 深部回环 / 父证书失效。
    """
    instruments = [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "W", "name": "工作仪器"},
    ]
    certs = [
        {"id": "CG", "instrument_code": "G", "cert_number": "CG",
         "issued_at": "2024-06-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "CW",
         "issued_at": "2024-06-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": (depth + 1) * 0.001, "unit": "mm"},
    ]
    edges = []
    prev = "CW"
    for i in range(depth):
        cid = f"C{i:05d}"
        instruments.append({"code": f"I{i:05d}", "name": cid})
        certs.append(
            {"id": cid, "instrument_code": f"I{i:05d}", "cert_number": cid,
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": (depth - i) * 0.001, "unit": "mm"}
        )
        edges.append({"child_cert_id": prev, "parent_cert_id": cid})
        prev = cid
    if broken_link is None or broken_link[0] not in ("dead_end", "cycle"):
        edges.append({"child_cert_id": prev, "parent_cert_id": "CG"})
    if broken_link is not None:
        kind, at_depth = broken_link
        if kind == "cycle":
            # 最深证书不再向基准连边，改为回指链上祖先——唯一出路构成环
            edges.append(
                {"child_cert_id": f"C{at_depth:05d}", "parent_cert_id": "C00005"}
            )
        elif kind == "inactive_parent":
            # 深部父证书在查询时点失效（其后链路不再可达）
            target = next(c for c in certs if c["id"] == f"C{at_depth:05d}")
            target["valid_from"] = "2027-01-01T00:00:00"
            target["valid_until"] = "2028-01-01T00:00:00"
    return instruments, certs, edges


def _submit_deep_chain(client, depth=DEPTH, broken_link=None):
    instruments, certs, edges = _deep_chain_payload(depth, broken_link)
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments, "certificates": certs, "edges": edges},
    )
    assert r.status_code == 201, r.text


def test_deep_chain_beyond_recursion_limit_resolves_unique(client: TestClient):
    """单条深链（深度 > 递归上限）逐级连到国家基准：正常成链，不再 500。"""
    assert DEPTH > sys.getrecursionlimit()
    _submit_deep_chain(client)

    for url in (f"/api/trace/W?at={AT}", f"/api/reports/W?at={AT}"):
        r = client.get(url)
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["resolution"] == "unique"
        assert report["valid"] is True
        assert report["error_count"] == 0
        chain = report["active_chain"]
        assert chain["reaches_national_standard"] is True
        ids = [n["certificate_id"] for n in chain["nodes"]]
        assert ids[0] == "CW" and ids[-1] == "CG"
        assert len(ids) == DEPTH + 2
        assert report["statistics"]["active_chain_depth"] == DEPTH + 2


def test_deep_chain_common_traceability_no_500(client: TestClient):
    """共用报告接口（共同溯源核查）对深链仪器返回 200 而非 500。"""
    _submit_deep_chain(client)
    r = client.post(
        "/api/assessments/common-traceability",
        json={"at": AT_Z, "instrument_codes": ["W", "G"]},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["common_certificate_found"] is True
    assert d["common_certificate_id"] == "CG"
    assert d["participants"] == ["W", "G"]
    row = next(x for x in d["instrument_chains"] if x["instrument"] == "W")
    assert row["participated"] is True
    assert row["distance"] == DEPTH + 1
    assert row["chain_certificate_ids"][0] == "CW"
    assert row["chain_certificate_ids"][-1] == "CG"


def test_deep_chain_validation_conflict_reported_at_deep_node(client: TestClient):
    """深链中部的不确定度收缩仍按现有校验规则检出并指向深层节点。"""
    instruments, certs, edges = _deep_chain_payload()
    # 把深层证书 C01100 的不确定度抬到其子证书之上 -> 沿链收缩
    deep_id = f"C{(DEPTH - 100):05d}"
    next(c for c in certs if c["id"] == deep_id)["uncertainty"] = 5.0
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments, "certificates": certs, "edges": edges},
    )
    assert r.status_code == 201, r.text

    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "broken"
    assert report["valid"] is False
    assert any(
        c["code"] == "ALL_CHAINS_FAILED_CHECKS" for c in report["conflicts"]
    )
    regressions = [
        c for c in report["conflicts"] if c["code"] == "UNCERTAINTY_REGRESSION"
    ]
    assert len(regressions) == 1
    assert regressions[0]["node"] == f"C{(DEPTH - 101):05d}"
    assert regressions[0]["detail"]["parent_cert_id"] == deep_id
    # 链虽到基准但校验失败：候选链保留完整深度
    assert len(report["candidates"]) == 1
    assert len(report["candidates"][0]["nodes"]) == DEPTH + 2


def test_deep_chain_dead_end_termination_recorded(client: TestClient):
    """深链末端无向上边：DEAD_END 终止记录指向最深证书，判定断裂。"""
    _submit_deep_chain(client, broken_link=("dead_end", DEPTH - 1))
    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "broken"
    dead_ends = [c for c in report["conflicts"] if c["code"] == "DEAD_END"]
    assert len(dead_ends) == 1
    assert dead_ends[0]["node"] == f"C{(DEPTH - 1):05d}"
    assert any(c["code"] == "CHAIN_BROKEN" for c in report["conflicts"])
    assert len(report["broken_chains"]) == 1
    # 断裂链完整保留到最深节点
    ids = [n["certificate_id"] for n in report["broken_chains"][0]["nodes"]]
    assert ids[-1] == f"C{(DEPTH - 1):05d}"
    assert len(ids) == DEPTH + 1


def test_deep_chain_cycle_termination_recorded(client: TestClient):
    """深部回环：CYCLE_DETECTED 终止记录指向重复节点，判定断裂。"""
    _submit_deep_chain(client, broken_link=("cycle", DEPTH - 1))
    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "broken"
    cycles = [c for c in report["conflicts"] if c["code"] == "CYCLE_DETECTED"]
    assert len(cycles) == 1
    assert cycles[0]["node"] == "C00005"
    broken = report["broken_chains"]
    assert any(c["cycle"] is True for c in broken)


def test_deep_chain_inactive_parent_termination_recorded(client: TestClient):
    """深部父证书在查询时点失效：PARENT_CERT_NOT_ACTIVE 终止记录。"""
    _submit_deep_chain(client, broken_link=("inactive_parent", DEPTH - 100))
    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "broken"
    inactive = [
        c for c in report["conflicts"] if c["code"] == "PARENT_CERT_NOT_ACTIVE"
    ]
    assert len(inactive) == 1
    assert inactive[0]["node"] == f"C{(DEPTH - 100):05d}"
    assert any(c["code"] == "CHAIN_BROKEN" for c in report["conflicts"])


def test_deep_valid_chain_then_fan_truncation_is_undetermined(client: TestClient):
    """深链合格 + 后续死路分叉推过 500 上限：判 undetermined 而非唯一。

    深链首跳 CA* 字典序先于死路父证书 CB*，深链先被枚举为唯一合格链；
    随后 503 条死路填满上限——已见合格链不足 2 条，不得把样本判为唯一。
    """
    instruments, certs, edges = _deep_chain_payload(depth=600)
    # 深链中间证书 id 形如 C00000；让首跳之外的深链证书字典序先于 CB 即可，
    # 这里直接给起点 CW 追加 503 条死路边（父证书 CB* 排在 C0* 之后）。
    for i in range(503):
        instruments.append({"code": f"B{i}", "name": f"B{i}"})
        certs.append(
            {"id": f"CB{i}", "instrument_code": f"B{i}", "cert_number": f"CB{i}",
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.5, "unit": "mm"}
        )
        edges.append({"child_cert_id": "CW", "parent_cert_id": f"CB{i}"})
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments, "certificates": certs, "edges": edges},
    )
    assert r.status_code == 201, r.text

    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "undetermined"
    assert report["valid"] is False
    assert report["active_chain"] is None  # 未定不提供有效主链
    assert report["statistics"]["paths_truncated"] is True
    assert report["statistics"]["paths_enumerated"] == 500
    assert report["statistics"]["valid_root_chains"] == 1
    codes = {c["code"] for c in report["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "TOO_MANY_CANDIDATES" in codes
    # 不得把样本误判为唯一或断裂
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    # 已枚举的深链合格样本保留在 candidates 中（但不作为主链）
    assert len(report["candidates"]) == 1
    sample_ids = [
        n["certificate_id"] for n in report["candidates"][0]["nodes"]
    ]
    assert sample_ids[0] == "CW" and sample_ids[-1] == "CG"
    assert len(sample_ids) == 602
    assert len(report["broken_chains"]) == 499
