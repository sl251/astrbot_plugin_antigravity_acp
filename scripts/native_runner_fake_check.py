#!/usr/bin/env python3
"""Real AstrBot runner/executor + deterministic fake ACP (no model/network).
Run in installed AstrBot with repo tests/fake_tool_acp.py available.
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, '/AstrBot')


async def main(root):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import AstrMessageEvent, event_context
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.context import Context

    from agy_provider import host_compat, sessions
    from agy_provider.config import Settings
    from agy_provider.process import LaunchInfo
    from agy_provider.provider import AgyProvider, configure

    results = []
    settings = Settings(enable_tools=True, startup_timeout=5, request_timeout=15,
                        tool_execution_timeout=5, max_concurrent=2)
    configure(settings)
    class Event(AstrMessageEvent):
        async def send(self, msg):
            self.captured.append([type(c).__name__ for c in msg.chain])
            self._has_send_oper = True

    def make_event():
        m = AstrBotMessage()
        m.type = MessageType.FRIEND_MESSAGE
        m.self_id = 'fake-only'
        m.session_id = 'fake-only'
        m.message_id = 'fake-only'
        m.sender = MessageMember(user_id='fake-only', nickname='fake-only')
        m.message = [Plain(text='fixture')]
        m.message_str = 'fixture'
        m.raw_message = {}
        e = Event('fixture', m, PlatformMetadata(name='capture', description='no adapter', id='capture'), m.session_id)
        e.captured = []
        return e

    class Echo(FunctionTool):
        async def run(self, event, message):
            event.executed += 1
            return 'real-host-receipt:' + message

    class Direct(FunctionTool):
        async def run(self, event, message):
            event.executed += 1
            await event.send(event.plain_result('direct receipt'))
            return None

    class Hooks(BaseAgentRunHooks):
        async def on_tool_start(self, ctx, tool, tool_args):
            ctx.context.event.hook_start += 1
        async def on_tool_end(self, ctx, tool, tool_args, result):
            ctx.context.event.hook_end += 1

    fake = REPO / 'tests/fake_tool_acp.py'
    assert fake.is_file(), fake
    host_compat.install_host_compat()
    try:
        with patch.object(sessions, 'launch_args', lambda info, home, use_default_agent=False:
                          [sys.executable, str(fake), '--log', str(root/'acp.jsonl')]):
            for mode, cls, missing_identity in [('full', Echo, False), ('skills_like', Echo, True), ('full', Direct, True)]:
                event = make_event()
                event.executed = event.hook_start = event.hook_end = 0
                ctx = ContextWrapper(context=AstrAgentContext(context=Context.__new__(Context), event=event), tool_call_timeout=5)
                tool = cls(name='echo_probe', description='Fixture tool', parameters={'type':'object','properties':{'message':{'type':'string'}},'required':['message'],'additionalProperties':False})
                provider = AgyProvider({'id':'fixture','type':'agy_acp','provider':'agy_acp','model':'gemini-test'}, {})
                provider.runtime.root = root / (mode + cls.__name__)
                provider.runtime.info = LaunchInfo(sys.executable, 'fixture', False)
                req = ProviderRequest(prompt='fixture-'+cls.__name__, session_id='' if missing_identity else 'fixture-session', contexts=[], model='gemini-test', func_tool=ToolSet([tool]))
                runner = ToolLoopAgentRunner()
                try:
                    with event_context(event):
                        await runner.reset(provider=provider, request=req, run_context=ctx, tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(), streaming=False, tool_schema_mode=mode, fallback_providers=[], request_max_retries=1)
                        async with asyncio.timeout(20):
                            async with contextlib.aclosing(runner.step_until_done(4)) as stream:
                                async for _ in stream:
                                    pass
                    row = {'requested_mode':mode, 'effective_mode':runner.tool_schema_mode, 'tool':cls.__name__, 'missing_identity':missing_identity, 'assigned_identity':bool(runner.req.session_id), 'executed':event.executed,'hook_start':event.hook_start,'hook_end':event.hook_end,'captures':len(event.captured),'pending_turns':len(provider.runtime.turns),'callers':provider.runtime.callers,'outcomes':list(provider.runtime.tool_outcomes.values()), 'done':runner.done()}
                    if cls is Echo:
                        final=runner.get_final_llm_resp()
                        row['real_receipt_readback']=bool(final and 'real-host-receipt:' in final.completion_text)
                    assert row['effective_mode']=='full', row
                    assert row['assigned_identity'], row
                    assert row['executed']==row['hook_start']==row['hook_end']==1, row
                    assert row['pending_turns']==row['callers']==0 and row['done'], row
                    if cls is Direct:
                        assert row['captures']==1 and row['outcomes']==['unknown'], row
                    else:
                        assert row['real_receipt_readback'] and row['outcomes']==['completed'], row
                    row['status']='PASS'
                    results.append(row)
                finally:
                    runner.request_stop()
                    await provider.terminate()
    finally:
        host_compat.uninstall_host_compat()
    print(json.dumps({'status':'PASS','real_native_runner':True,'real_native_executor':True,'fake_acp':True,'model_requests':0,'platform_sends':0,'cases':results},ensure_ascii=False))


if __name__=='__main__':
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='native-fake-') as d:
        os.environ['ASTRBOT_ROOT']=str(Path(d)/'state')
        asyncio.run(main(Path(d)))
