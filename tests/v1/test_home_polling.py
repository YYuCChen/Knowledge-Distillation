"""Run the actual event handlers with deferred network replies.

Fixture correction (Q11, 2026-09-29): since 13d6018 the submit handler asks
form.closest('[data-stale-confirmation]') and button.closest(...). The old stub
closest:()=>true made every form look like a stale card, so no POST was sent.
The stubs now answer only for '#home-results', and the fake applyPage models the
server render that owns the button after an accepted request (BUG-20260916-01).
The fake is installed inside the context: on Node 26 assigning context.applyPage
from outside no longer replaces the script's own function declaration.
"""
import subprocess
from pathlib import Path


def test_poll_in_flight_never_discards_submit_or_overwrites_its_result():
    script=Path('src/knowledge_distiller/v1/static/home.js').resolve()
    harness=r'''
const vm=require('vm'), fs=require('fs'), assert=require('assert');
const handlers={},requests=[],applied=[];
const feedback={textContent:"",hidden:true,setAttribute(){}}, organization={textContent:"",dataset:{}};
const context={console,Map,Array,Error,AbortController,setTimeout(){},clearTimeout(){},FormData:class{append(){}},
 document:{getElementById:()=>null,querySelector:s=>s==='#home-results'?{}:s==='#confirmation-action-feedback'?feedback:s==='.organization-feedback'?organization:null,querySelectorAll:()=>[],
 addEventListener:(name,fn)=>{handlers[name]=fn},fonts:{ready:Promise.resolve()}},
 window:{setTimeout(){},addEventListener(){},location:{href:'/'},kdDialog:async()=>true},
 localStorage:{getItem(){return null}},ResizeObserver:class{observe(){}disconnect(){}},
 fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.__applied=applied;
vm.runInContext('applyPage=(html)=>{__applied.push(html);__button.disabled=false;}',context);
(async()=>{
const poll=context.pollStatus();assert.equal(requests.length,1);
const button=context.__button={disabled:false,textContent:'采用',getAttribute:()=>null,closest:()=>null};
const form={id:'candidate-1',matches:()=>false,closest:s=>s==='#home-results'?{}:null,getAttribute:()=>'/confirm'};
const submit=handlers.submit({target:form,submitter:button,preventDefault(){}});
await Promise.resolve();assert.equal(requests.length,2,'click during polling must issue POST');
assert.equal(feedback.textContent,'');assert.equal(feedback.hidden,true);assert.equal(organization.textContent,'');
requests[1].resolve({ok:true,status:200,text:async()=>'saved'});await submit;
requests[0].resolve({ok:true,status:200,text:async()=>'stale'});await poll;
assert.deepEqual(applied,['saved'],'stale poll must not replace saved result');
assert.equal(button.disabled,false);assert.equal(feedback.textContent,'');assert.equal(feedback.hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',harness,str(script)],text=True,capture_output=True)
    assert result.returncode==0,result.stderr
