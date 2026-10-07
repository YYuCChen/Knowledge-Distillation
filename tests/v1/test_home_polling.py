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


def test_same_state_wiki_retry_is_reconciled_as_an_accepted_submission():
    script=Path('src/knowledge_distiller/v1/static/home.js').resolve()
    harness=r'''
const vm=require('vm'), fs=require('fs'), assert=require('assert');
class Element {
 constructor(name,attrs={},children=[]){this.nodeType=1;this.nodeName=name.toUpperCase();this._attrs=new Map(Object.entries(attrs));this.childNodes=[];this.elements={};for(const child of children)this.append(child)}
 get id(){return this.getAttribute('id')||''} get dataset(){const out={};for(const [name,value] of this._attrs)if(name.startsWith('data-'))out[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;return out}
 get attributes(){return [...this._attrs].map(([name,value])=>({name,value}))} get firstChild(){return this.childNodes[0]||null}
 get nextSibling(){if(!this.parentNode)return null;const at=this.parentNode.childNodes.indexOf(this);return this.parentNode.childNodes[at+1]||null}
 get innerHTML(){return 'same-state'} get disabled(){return this.hasAttribute('disabled')} set disabled(value){value?this.setAttribute('disabled',''):this.removeAttribute('disabled')}
 append(child){child.parentNode=this;this.childNodes.push(child)}
 hasAttribute(name){return this._attrs.has(name)} getAttribute(name){return this._attrs.get(name)??null}
 setAttribute(name,value){this._attrs.set(name,String(value))} removeAttribute(name){this._attrs.delete(name)}
 matches(selector){return selector==='form' ? this.nodeName==='FORM' : selector==='audio' ? false : selector==='details' ? false : false}
 querySelector(selector){return selector==='[data-stale-confirmation]'?null:null}
 querySelectorAll(selector){if(selector==='form[id]'&&this.nodeName==='SECTION')return this.childNodes.filter(node=>node.nodeName==='FORM'&&node.id);return []}
 closest(selector){if(selector==='#home-results')return root;if(selector==='[data-stale-confirmation]'||selector==='.todo-card-shell'||selector==='[data-sync-key]')return null;return null}
 cloneNode(deep){return new Element(this.nodeName,Object.fromEntries(this._attrs),deep?this.childNodes.map(node=>node.cloneNode(true)):[])}
 insertBefore(child,before){if(child.parentNode){const old=child.parentNode.childNodes.indexOf(child);if(old>=0)child.parentNode.childNodes.splice(old,1)}child.parentNode=this;const at=before?this.childNodes.indexOf(before):-1;this.childNodes.splice(at<0?this.childNodes.length:at,0,child)}
 remove(){if(!this.parentNode)return;const at=this.parentNode.childNodes.indexOf(this);if(at>=0)this.parentNode.childNodes.splice(at,1);this.parentNode=null}
 replaceWith(node){this.parentNode?.insertBefore(node,this);this.remove()}
}
const currentButton=new Element('button'),currentForm=new Element('form',{id:'wiki-retry',action:'/organization/'+('a'.repeat(32))+'/retry'},[currentButton]);
const root=new Element('section',{},[currentForm]);
const serverButton=new Element('button'),serverForm=new Element('form',{id:'wiki-retry',action:currentForm.getAttribute('action')},[serverButton]);
const nextRoot=new Element('section',{},[serverForm]);
const handlers={},requests=[];
const context={console,Map,Array,Error,AbortController,setTimeout(){},clearTimeout(){},FormData:class{append(){}},
 Node:{ELEMENT_NODE:1},CSS:{escape:value=>value},DOMParser:class{parseFromString(){return{querySelector:s=>s==='#home-results'?nextRoot:null}}},
 document:{getElementById:id=>id==='wiki-retry'?currentForm:null,querySelector:s=>s==='#home-results'?root:null,querySelectorAll:()=>[],activeElement:null,
 addEventListener:(name,fn)=>{handlers[name]=fn},fonts:{ready:Promise.resolve()}},
 window:{setTimeout(){},addEventListener(){},location:{href:'/'},kdDialog:async()=>true},
 localStorage:{getItem(){return null}},ResizeObserver:class{observe(){}disconnect(){}},
 fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
currentButton.textContent='继续整理';currentButton.name='';
const submit=handlers.submit({target:currentForm,submitter:currentButton,preventDefault(){}});
await Promise.resolve();assert.equal(requests.length,1);assert.equal(currentButton.disabled,true);
requests[0].resolve({ok:true,status:200,text:async()=>'same-state'});await submit;
assert.equal(currentButton.disabled,false,'the server-rendered enabled state must replace the in-flight disable');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',harness,str(script)],text=True,capture_output=True)
    assert result.returncode==0,result.stderr
