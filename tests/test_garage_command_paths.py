"""Execute the real V1 entry methods against a recorded transport."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

ROOT=Path(__file__).resolve().parents[1]/'custom_components/roborock_plus'


class HAError(Exception):
    def __init__(self,*args,**kwargs):
        super().__init__(args or kwargs.get('translation_placeholders',{}).get('reason','garage_error'))


def vacuum_class():
    tree=ast.parse((ROOT/'vacuum.py').read_text(encoding='utf-8'))
    original=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RoborockVacuum')
    methods=[n for n in original.body if isinstance(n,ast.AsyncFunctionDef) and n.name in (
        'send','_async_guard_clean_command','async_request_garage_close','async_reset_garage_control',
        'async_start','async_resume_task')]
    assert any(n.name=='send' for n in methods), 'V1 transport has no managed garage interlock'
    class Base:
        async def send(self,command,params=None):
            self.events.append(('transport',command,params))
            return {'accepted':True}
    class ControlError(ValueError):
        def __init__(self,reason):
            self.reason=reason
            super().__init__(reason)
    def ownership(hass,options,duid):
        if hass.owner is not None and hass.owner!=duid:
            raise ControlError('duplicate_binding')
    namespace=dict(Base=Base,Any=Any,RoborockCommand=str,HomeAssistantError=HAError,ServiceValidationError=HAError,
                   DOMAIN='roborock_plus',GarageControlError=ControlError,GarageTaskError=ControlError,
                   is_garage_configured=lambda c:c.managed,check_legacy_ownership=ownership,
                   should_guard_clean_command=lambda c:c=='app_start',
                   select_resume_command_for_clean_command=lambda c:'app_resume',
                   command_name=lambda c:c,command_value=lambda c:c)
    async def legacy(*args,**kwargs):
        args[0].events.append(('legacy_open',))
    namespace['async_guard_garage_open']=legacy
    resume_namespace = {}
    exec(compile((ROOT/'resume_logic.py').read_text(encoding='utf-8'), str(ROOT/'resume_logic.py'), 'exec'), resume_namespace)
    namespace.update({key: resume_namespace[key] for key in ('select_resume_command', 'select_start_or_resume_command')})
    wrapper=ast.ClassDef(name='RoborockVacuum',bases=[ast.Name(id='Base',ctx=ast.Load())],
                         keywords=[],body=methods,decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(ROOT/'vacuum.py'),'exec'),namespace)
    return namespace['RoborockVacuum'],ControlError


def instance(managed=True):
    cls,err=vacuum_class()
    entity=cls()
    entity.events=[]
    entity.hass=NS(owner=None,events=entity.events)
    entity.coordinator=NS(managed=managed,duid='robot',garage_runtime=None,garage_setup_error=None,
                          config_entry=NS(options={}),garage_door_echo_window=None)
    return entity,err


@pytest.mark.parametrize('command',['app_start','app_spot','app_segment_clean','app_zoned_clean',
                                    'app_goto_target','app_resume','app_charge','opaque_move'])
def test_every_transport_entry_is_delegated_to_the_runtime(command):
    e,_=instance()
    async def send(cmd,callback):
        e.events.append(('runtime',cmd))
        return await callback()
    e.coordinator.garage_runtime=NS(async_send=send)
    result=asyncio.run(e.send(command,[1]))
    assert e.events==[('runtime',command),('transport',command,[1])]
    assert result=={'accepted':True}


def test_enabled_but_failed_runtime_cannot_fall_back_to_unguarded_send():
    e,_=instance()
    e.coordinator.garage_setup_error='duplicate_binding'
    with pytest.raises(HAError):
        asyncio.run(e.send('app_start'))
    assert not e.events


def test_legacy_ownership_is_checked_before_old_guard_opens_door():
    e,_=instance(managed=False)
    e.hass.owner='other'
    with pytest.raises(Exception,match='duplicate_binding'):
        asyncio.run(e._async_guard_clean_command('app_start'))
    assert ('legacy_open',) not in e.events


def test_new_binding_does_not_run_legacy_door_wait_before_managed_send():
    e,_=instance()
    asyncio.run(e._async_guard_clean_command('app_start'))
    assert ('legacy_open',) not in e.events


def test_legacy_robot_keeps_existing_transport_without_new_binding():
    e,_=instance(managed=False)
    assert asyncio.run(e.send('app_start'))=={'accepted':True}
    assert e.events==[('transport','app_start',None)]


def test_real_resume_entry_uses_reconciled_session_after_restart_with_cleared_flags():
    from test_garage_runtime import Boundaries
    b = Boundaries(initialized=True)
    runtime = b.create()
    runtime._restored, runtime._first_install = True, False
    runtime.session.begin(b.summary, b.utc, 'app_start')
    b.status.state.name, b.status.in_cleaning = 'cleaning', 1
    b.advance()
    runtime.controller.refresh()
    payload = runtime.session.to_dict()
    runtime.session.restore(payload)
    runtime.controller.fault('session_recovery_required')
    b.status.state.name, b.status.in_cleaning = 'paused', 0
    b.coord.last_resume_command = None
    b.advance()
    runtime.reset()
    e, _ = instance()
    e.coordinator = b.coord
    b.coord.managed, b.coord.garage_runtime, b.coord.garage_setup_error = True, runtime, None
    b.hass.async_create_task = lambda coroutine, **kwargs: asyncio.create_task(coroutine)
    e.hass, e._status_trait = b.hass, b.status
    asyncio.run(e.async_resume_task())
    assert e.events == [('transport', 'app_resume', None)]
    assert runtime.session.session_id == payload['session_id']
    assert runtime.controller.phase == 'awaiting_start'
