from trading.ths import visual


def _detection(text, x, y):
    return {"text": text, "score": 0.99, "x": x, "y": y, "box": []}


def test_activity_candidate_rejects_holdings_page(monkeypatch, tmp_path):
    detections = [
        _detection("\u8bc1\u5238\u4ee3\u7801", 40, 18),
        _detection("\u80a1\u7968\u4f59\u989d", 280, 18),
        _detection("\u53ef\u7528\u4f59\u989d", 370, 18),
        _detection("\u51bb\u7ed3\u6570\u91cf", 470, 18),
        _detection("\u5e02\u4ef7", 620, 18),
        _detection("\u5e02\u503c", 1200, 18),
        _detection("600000", 40, 58),
    ]
    monkeypatch.setattr(visual, "_detections", lambda path: (detections, [0.01]))
    image = tmp_path / "holdings.png"
    image.write_bytes(b"image")

    result = visual.read_grid_candidate(
        image,
        {"kind": "activity", "x_centers": [40, 145, 280, 370]},
        kind="orders",
    )

    assert result["kind"] == "orders"
    assert result["mapping_kind"] == "activity"
    assert result["header_semantics"]["status"] == "page_header_mismatch"
    assert result["candidate_state"] == "page_header_mismatch"
    assert result["semantic_verified"] is False


def test_activity_candidate_accepts_matching_activity_header(monkeypatch, tmp_path):
    detections = [
        _detection("\u59d4\u6258\u65f6\u95f4", 40, 18),
        _detection("\u59d4\u6258\u4ef7\u683c", 145, 18),
        _detection("\u6210\u4ea4\u6570\u91cf", 280, 18),
        _detection("\u59d4\u6258\u72b6\u6001", 370, 18),
        _detection("09:30", 40, 58),
        _detection("12.34", 145, 58),
        _detection("100", 280, 58),
        _detection("\u5df2\u6210", 370, 58),
    ]
    monkeypatch.setattr(visual, "_detections", lambda path: (detections, [0.01]))
    image = tmp_path / "orders.png"
    image.write_bytes(b"image")

    result = visual.read_grid_candidate(
        image,
        {"kind": "activity", "x_centers": [40, 145, 280, 370]},
        kind="orders",
    )

    assert result["header_semantics"]["status"] == "header_verified"
    assert result["header_semantic_verified"] is True
    assert result["candidate_state"] == "header_only_no_symbol_rows"
