import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import {OwnedPage} from './owned-page.mjs';
export async function readerPage(root, platform, boundContext, endpoint) {
  if (endpoint) {
    if (!/^owned:[a-f0-9]{32}$/.test(boundContext || '') || !/^ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\//.test(endpoint))
      throw Error(platform + '_connection_changed');
    return {page:await OwnedPage.create(endpoint),contextId:boundContext};
  }
  const base = pathToFileURL(root + '/dist/src/browser/');
  const {Page} = await import(new URL('page.js',base));
  const {fetchDaemonStatus} = await import(new URL('daemon-transport.js',base));
  const options = boundContext ? {contextId:boundContext} : {};
  let status = await fetchDaemonStatus(options);
  if (!status) {
    // Start only when absent. Never replace another application's live daemon.
    const {spawnDaemonProcess} = await import(new URL('daemon-lifecycle.js', base));
    let launchError = false;
    try {
      const process = spawnDaemonProcess();
      process.once('error', () => { launchError = true; });
    } catch { launchError = true; }
    const deadline = Date.now() + 5000;
    while (!status && !launchError && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 200));
      status = await fetchDaemonStatus(options);
    }
    if (!status) throw Error(platform + '_bridge_start_failed');
  }
  // Give an already installed extension a short chance to reconnect after boot.
  if (!status.extensionConnected && !status.profileRequired && !status.profileDisconnected) {
    const deadline = Date.now() + 3000;
    while (!status?.extensionConnected && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 200));
      status = await fetchDaemonStatus(options);
      if (status?.profileRequired || status?.profileDisconnected) break;
    }
  }
  if (!status) throw Error(platform + '_bridge_start_failed');
  if (status.profileRequired) throw Error(platform + '_bridge_profile_required');
  if (status.profileDisconnected) throw Error(platform + '_bridge_profile_disconnected');
  if (!status.extensionConnected) throw Error(platform + '_bridge_extension_required');
  if (!status.contextId) throw Error(platform + '_bridge_profile_required');
  const contextId = boundContext || status.contextId;
  if(status.contextId !== contextId) throw Error(platform + '_connection_changed');
  return {page:new Page('kd-' + platform + '-' + randomUUID(),60,contextId,['zhihu', 'xiaohongshu'].includes(platform) ? 'foreground' : 'background','adapter','ephemeral'),contextId};
}
