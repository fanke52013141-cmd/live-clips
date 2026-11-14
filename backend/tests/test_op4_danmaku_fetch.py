"""OP-4 弹幕自动抓取：BV 解析、view API、deflate 弹幕解压、开关与失败回退、选段接线。"""

import zlib
from pathlib import Path

import httpx
import pytest

from backend.utils import bilibili_danmaku
from backend.utils.danmaku_parser import DanmakuEvent


def test_recall_enabled_default_on_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv('AUTOCLIP_DANMAKU_RECALL', raising=False)
    assert bilibili_danmaku.recall_enabled() is True
    monkeypatch.setenv('AUTOCLIP_DANMAKU_RECALL', '0')
    assert bilibili_danmaku.recall_enabled() is False


def test_extract_bvid_from_url_forms():
    assert bilibili_danmaku.extract_bvid('https://www.bilibili.com/video/BV1qheu6kEFV/?p=1') == 'BV1qheu6kEFV'
    assert bilibili_danmaku.extract_bvid('https://b23.tv/abc123') is None  # 非直链且网络失败时为 None


def test_extract_bvid_resolves_short_link(monkeypatch):
    class FakeResponse:
        headers = {'location': 'https://www.bilibili.com/video/BV1qheu6kEFV/'}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(bilibili_danmaku.httpx, 'Client', FakeClient)
    assert bilibili_danmaku.extract_bvid('https://b23.tv/abc123') == 'BV1qheu6kEFV'


def test_extract_bvid_rejects_non_bilibili():
    assert bilibili_danmaku.extract_bvid('https://www.youtube.com/watch?v=x') is None
    assert bilibili_danmaku.extract_bvid('') is None


def test_fetch_view_info_parses_cid(monkeypatch):
    class FakeResponse:
        content = b''

        def json(self):
            return {'code': 0, 'data': {'cid': 12345, 'duration': 3600, 'title': '测试视频'}}

    monkeypatch.setattr(bilibili_danmaku.httpx, 'get', lambda *a, **k: FakeResponse())
    info = bilibili_danmaku.fetch_view_info('BV1qheu6kEFV')
    assert info == {'cid': 12345, 'duration': 3600, 'title': '测试视频'}


def test_fetch_view_info_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError('refused')
    monkeypatch.setattr(bilibili_danmaku.httpx, 'get', boom)
    assert bilibili_danmaku.fetch_view_info('BV1qheu6kEFV') is None


_DANMAKU_XML = ('<?xml version="1.0"?><i><chatserver>chat</chatserver>'
                '<d p="40.0,1,25,0,0,0,10001,0">前排</d>'
                '<d p="41.5,1,25,0,0,0,10002,0">666</d>'
                '<d p="42.0,1,25,0,0,0,10001,0">名场面</d></i>')


def test_fetch_danmaku_xml_deflates_and_parses(monkeypatch):
    class FakeResponse:
        content = zlib.compress(_DANMAKU_XML.encode('utf-8'))

    monkeypatch.setattr(bilibili_danmaku.httpx, 'get', lambda *a, **k: FakeResponse())
    xml = bilibili_danmaku.fetch_danmaku_xml(12345)
    assert '名场面' in xml


def test_fetch_events_for_url_happy_path(monkeypatch):
    class FakeResponse:
        def __init__(self, content, payload=None):
            self.content = content
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if 'web-interface/view' in str(url):
            return FakeResponse(b'', payload={'code': 0, 'data': {'cid': 12345, 'duration': 3600,
                                                                  'title': '测试视频'}})
        return FakeResponse(zlib.compress(_DANMAKU_XML.encode('utf-8')))

    monkeypatch.setattr(bilibili_danmaku.httpx, 'get', fake_get)
    events = bilibili_danmaku.fetch_events_for_url('https://www.bilibili.com/video/BV1qheu6kEFV/', max_time=41.8)
    assert [e.time for e in events] == [40.0, 41.5]  # 42.0 超出视频时长被丢弃


def test_fetch_events_for_url_disabled_or_non_bilibili(monkeypatch):
    monkeypatch.setenv('AUTOCLIP_DANMAKU_RECALL', '0')
    assert bilibili_danmaku.fetch_events_for_url('https://www.bilibili.com/video/BV1qheu6kEFV/') is None
    monkeypatch.setenv('AUTOCLIP_DANMAKU_RECALL', '1')
    assert bilibili_danmaku.fetch_events_for_url('https://www.youtube.com/watch?v=x') is None


def test_fetch_events_for_url_network_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError('refused')
    monkeypatch.setattr(bilibili_danmaku.httpx, 'get', boom)
    assert bilibili_danmaku.fetch_events_for_url('https://www.bilibili.com/video/BV1qheu6kEFV/') is None


def test_adapter_wires_danmaku_into_find_clips(monkeypatch, tmp_path: Path):
    """_find_clips_in_one_pass 把抓到的弹幕事件传给 find_clips；抓不到传 None。"""
    from backend.services.simple_pipeline_adapter import SimplePipelineAdapter

    class FakeTask:
        id = 'task-1'

    adapter = SimplePipelineAdapter.__new__(SimplePipelineAdapter)
    adapter.project_id = 'p1'
    adapter.task_id = 'task-1'
    monkeypatch.setattr(adapter, '_project_source_url', lambda: 'https://www.bilibili.com/video/BV1qheu6kEFV/')
    srt = tmp_path / 'input.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:05,000\n第一句\n2\n00:00:05,000 --> 00:00:10,000\n第二句\n', encoding='utf-8')

    events = [DanmakuEvent(time=1.0)]
    captured = {}

    def fake_find_clips(entries, call, *, threshold, metadata_dir, danmaku_events=None, **kwargs):
        captured['danmaku'] = danmaku_events
        captured['entries'] = entries
        return []

    monkeypatch.setattr('backend.pipeline.clip_finder.find_clips', fake_find_clips)
    monkeypatch.setattr(adapter, '_danmaku_events_for', lambda entries: events)
    adapter._find_clips_in_one_pass(srt, tmp_path)
    assert captured['danmaku'] is events

    monkeypatch.setattr(adapter, '_danmaku_events_for', lambda entries: None)
    adapter._find_clips_in_one_pass(srt, tmp_path)
    assert captured['danmaku'] is None
