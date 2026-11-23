def test_workbench_exposes_actions_and_both_samples(client):
    response = client.get('/')
    assert response.status_code == 200
    assert all(text in response.text for text in ['期刊到藏校对册', '证据寄送', '归档本次勾稽', '十一月补刊清单', '验收清册'])
    assert client.get('/static/desk.js').status_code == 200


def test_workbench_exposes_handover_panel(client):
    response = client.get('/')
    assert response.status_code == 200
    assert all(text in response.text for text in [
        '停复刊交班', '送达本批来函', '停刊', '已恢复', '仍待确认', '来函台账'])
    assert client.get('/static/handover.js').status_code == 200
    assert client.get('/static/handover.css').status_code == 200
