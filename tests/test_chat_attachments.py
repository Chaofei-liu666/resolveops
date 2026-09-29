import base64

from production.chat_attachments import decode_attachments
from production import local_file_tool


def test_attachment_text_is_ephemeral_and_redacts_secret_values():
    attachment = decode_attachments([{
        'filename': 'notes.txt',
        'content_type': 'text/plain',
        'data_base64': base64.b64encode(b'API_KEY=secret-value\nnormal note').decode(),
    }])[0]

    assert attachment.filename == 'notes.txt'
    assert 'secret-value' not in attachment.text
    assert 'normal note' in attachment.text
    assert attachment.audit_metadata()['kind'] == 'document'


def test_local_file_tool_selects_only_discovered_readable_file(tmp_path, monkeypatch):
    report = tmp_path / 'sales-report.txt'
    report.write_text('September fulfillment summary', encoding='utf-8')
    (tmp_path / '.env').write_text('API_KEY=not-for-agent', encoding='utf-8')
    monkeypatch.setattr(local_file_tool, '_search_roots', lambda: [tmp_path])

    tool = local_file_tool.LocalFileReadTool()
    candidates = tool.find_candidates('读取 sales report')
    attachments = tool.read_selected(candidates, [str(report), str(tmp_path / '.env')])

    assert [item.filename for item in attachments] == ['sales-report.txt']
    assert attachments[0].text == 'September fulfillment summary'
