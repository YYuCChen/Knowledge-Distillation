(()=>{
window.runSamples=async count=>{
 for(let i=0;i<count;i++){
  const top=document.querySelector('[data-sync-key="member-stable"]').getBoundingClientRect().top;
  const startAudio=probe.audio.currentTime;
  const state=await fetch('/advance').then(r=>r.json());
  await new Promise((resolve,reject)=>{const t=setInterval(()=>{if(Number(document.querySelector('#server-version').textContent)===state.revision){clearInterval(t);clearTimeout(timeout);resolve();}},10);const timeout=setTimeout(()=>{clearInterval(t);reject(new Error('poll timeout'));},6000);});
  probe.samples.push({ms:Date.now()-state.committed,sameAudio:probe.audio===document.querySelector('audio'),sameInput:probe.input===document.querySelector('input[name=value]'),draft:probe.input.value,focus:document.activeElement===probe.input,selection:[probe.input.selectionStart,probe.input.selectionEnd],anchorDelta:document.querySelector('[data-sync-key="member-stable"]').getBoundingClientRect().top-top,playing:!probe.audio.paused,audioDelta:probe.audio.currentTime-startAudio});
 }
 return probe.samples.slice(-count);
};
return true;
})()
