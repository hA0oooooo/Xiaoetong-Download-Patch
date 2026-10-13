'use strict';
// Own implementation. Runs in the user's installed Electron main process.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');

function readShopPage(target, headers, redirects=0) {
  return new Promise((resolve,reject)=>{
    const request = https.get(target,{headers,timeout:20000},response=>{
      const cookies=new Map((headers.Cookie||'').split(';').filter(Boolean).map(v=>{const at=v.indexOf('=');return [v.slice(0,at).trim(),v.slice(at+1)];}));
      for(const line of response.headers['set-cookie']||[]) {const pair=line.split(';')[0];const at=pair.indexOf('=');if(at>0)cookies.set(pair.slice(0,at),pair.slice(at+1));}
      headers={...headers,Cookie:[...cookies].map(([k,v])=>`${k}=${v}`).join('; ')};
      if ([301,302,303,307,308].includes(response.statusCode)) {
        response.resume();
        const next = new URL(response.headers.location,target);
        const appPrefix=target.hostname.split('.')[0];
        const regional = next.protocol==='https:' && next.hostname===`${appPrefix}.h5.xet.pomoho.com` && !next.port && !next.username && !next.password;
        if (redirects>=4 || (next.origin!==target.origin && !regional)) {const failure=Error('unsupported_shop_redirect');failure.redirect_origin=next.origin;failure.redirect_path=next.pathname;reject(failure);return;}
        readShopPage(next,headers,redirects+1).then(resolve,reject);return;
      }
      if (response.statusCode!==200) {response.resume();reject(Error('document_page_failed'));return;}
      const chunks=[];let size=0;
      response.on('data',chunk=>{size+=chunk.length;if(size>4*1024*1024){request.destroy(Error('oversized_shop_response'));return;}chunks.push(chunk);});
      response.on('error',reject);
      response.on('end',()=>resolve({html:Buffer.concat(chunks).toString('utf8'),path:target.pathname,origin:target.origin,cookie:headers.Cookie}));
    });
    request.on('timeout',()=>request.destroy(Error('shop_timeout')));
    request.on('error',reject);
  });
}

function shopPost(origin,endpoint,parameters,headers) {
  const body=JSON.stringify({bizData:parameters});
  return new Promise((resolve,reject)=>{
    const request=https.request(new URL(endpoint,origin),{method:'POST',timeout:20000,headers:{...headers,'Content-Type':'application/json','Content-Length':Buffer.byteLength(body)}},response=>{
      if(response.statusCode!==200){response.resume();reject(Error('shop_api_failed'));return;}
      const chunks=[];let size=0;
      response.on('data',chunk=>{size+=chunk.length;if(size>4*1024*1024){request.destroy(Error('oversized_shop_response'));return;}chunks.push(chunk);});
      response.on('error',reject);
      response.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch{reject(Error('shop_api_not_json'));}});
    });
    request.on('timeout',()=>request.destroy(Error('shop_timeout')));request.on('error',reject);request.end(body);
  });
}

const ENDPOINTS = new Set([
  '/xe.pc_client.course/platform.token.check/1.0.0',
  '/xe.pc_client.course/my.all.course.lists.get/3.0.1',
  '/xe.pc_client.course/my.course.pay.get/2.0.0',
  '/xe.pc_client.course.business.avoidlogin.e_course.resource_catalog_list.get/1.0.0',
  '/xe.pc_client.course.business.column.items.get/2.0.0',
  '/xe.pc_client.course.business.resource.available.get/2.0.0',
  '/xe.pc_client.course.business.video.detail_info.get/2.0.0',
]);

function pageRequest(endpoint, body) {
  return `(async () => {
    const sdk = window.XiaoeTongClientSdk;
    const token = await sdk?.getAppToken?.();
    if (!token) return {error:'login_required'};
    const response = await fetch('https://study.xiaoe-tech.com' + ${JSON.stringify(endpoint)}, {
      method:'POST', credentials:'omit', redirect:'error',
      signal:AbortSignal.timeout(20000),
      headers:{'Content-Type':'application/json',login_app:'eapppc',login_client:'pc',
        'App-Token':token,app_id:'apposolbh821040'},
      body:JSON.stringify({...${JSON.stringify(body)},platform:'pc_client'})
    });
    if (!response.ok) return {error:'http_error',status:response.status};
    return {payload:await response.json()};
  })()`;
}

async function findPage(electron) {
  for (const win of electron.BrowserWindow.getAllWindows()) {
    if (win.isDestroyed()) continue;
    const url = win.webContents.getURL().toLowerCase();
    if (!url.startsWith('xiaoetongclient://entrance/')) continue;
    if (await win.webContents.executeJavaScript('Boolean(window.XiaoeTongClientSdk?.getAppToken)')) return win.webContents;
  }
  throw Error('client_page_not_ready');
}

const shopSessions = new Map();
async function shopSessionToken(page,app_id,user_id) {
  const identity=`${app_id}:${user_id}`;
  const cached=shopSessions.get(identity);
  if (cached && cached.expires>Date.now()) {
    const check=await page.executeJavaScript(pageRequest('/xe.pc_client.course/kotoken.check/1.0.0',{app_id,user_id,token:cached.token}));
    if (String(check.payload?.code)==='0' && check.payload?.data?.is_valid) return cached.token;
    shopSessions.delete(identity);
  }
  const result=await page.executeJavaScript(pageRequest('/xe.pc_client.course/kotoken.create/1.0.0',{app_id,user_id}));
  const token=result.payload?.data?.token;
  if (String(result.payload?.code)!=='0' || typeof token?.value!=='string' || !token.value) throw Error('shop_session_unavailable');
  const seconds=Number(token.expires);
  shopSessions.set(identity,{token:token.value,expires:Date.now()+(Number.isFinite(seconds)&&seconds>0?seconds:60)*1000});
  return token.value;
}

function authorizedDocument(detail,resource_id) {
  if (String(detail?.code)!=='0') return {payload:{code:detail?.code??'missing'}};
  const data=detail.data;
  if (!data || data.resource_id!==resource_id || data.resource_type!==51) throw Error('unsupported_document_response');
  if (![true,1,'1'].includes(data.is_subscribe) || [true,1,'1'].includes(data.is_ban) || [true,1,'1'].includes(data.is_try)) return {payload:{code:'document_access_denied'}};
  return {payload:{code:0,data:{file_url:data.file_url,file_name:data.file_name,title:data.title}}};
}

async function dispatch(electron, message) {
  const page = await findPage(electron);
  if (message.action === 'status') {
    return page.executeJavaScript(`(async()=>({sdk:true,authenticated:Boolean(await window.XiaoeTongClientSdk.getAppToken())}))()`);
  }
  if (message.action === 'post') {
    if (!ENDPOINTS.has(message.endpoint) || !message.body || typeof message.body !== 'object' || Array.isArray(message.body)) throw Error('unsupported_request');
    return page.executeJavaScript(pageRequest(message.endpoint, message.body));
  }
  if (message.action === 'courseware') {
    const {app_id,user_id,course_id} = message;
    if (![app_id,user_id,course_id].every(value=>typeof value==='string' && value.length>0 && value.length<256)) throw Error('invalid_course_identity');
    const access=await page.executeJavaScript(pageRequest('/xe.pc_client.course.business.avoidlogin.e_course.resource_catalog_list.get/1.0.0',{
      app_id,user_id,buz_data:{resource_id:'',course_id,p_id:'0',order:'asc',page:1,page_size:20,sub_course_id:''},
    }));
    if (String(access.payload?.code)!=='0') return access;
    const flags=access.payload.data;
    if (![true,1,'1'].includes(flags?.available) || ![true,1,'1'].includes(flags?.subscribe) ||
        [true,1,'1'].includes(flags?.is_ban) || [true,1,'1'].includes(flags?.is_try)) return {payload:{code:'courseware_access_denied'}};
    const result=await page.executeJavaScript(pageRequest('/xe.pc_client.course.business.courseware_list.get/2.0.0',{
      app_id,user_id,buz_data:{resource_id:course_id,resource_type:50},
    }));
    if (String(result.payload?.code)!=='0') return result;
    if (!Array.isArray(result.payload.data)) throw Error('unsupported_courseware_response');
    const list=result.payload.data.filter(row=>[6,9].includes(Number(row.type)) && Number(row.operate_type)!==2 && typeof row.url==='string' && row.url)
      .map(row=>({material_id:row.material_id,title:row.title,type:Number(row.type),file_url:row.url}));
    return {payload:{code:0,data:{list}}};
  }
  if (message.action === 'video') {
    const {app_id, user_id, resource_id, course_id = ''} = message;
    if (![app_id, user_id, resource_id].every(value => typeof value === 'string' && value.length > 0 && value.length < 256) || typeof course_id !== 'string') throw Error('invalid_video_identity');
    const body = {app_id,user_id,buz_data:{resource_id,course_id}};
    // Server authorization is preserved. No fabricated purchase/availability response.
    const result = await page.executeJavaScript(pageRequest('/xe.pc_client.course.business.video.detail_info.get/2.0.0', body));
    if (!result.payload || String(result.payload.code) !== '0') return result;
    const data = result.payload.data;
    if (!data || !data.video_info || !data.video_urls) throw Error('unsupported_video_response');
    if (data.is_try === true || data.is_try === 1 || data.is_try === '1') throw Error('trial_video');
    const decoded = await page.executeJavaScript(`(async()=>JSON.parse(await window.XiaoeTongClientSdk.deobfuscateString(${JSON.stringify(data.video_urls)})))()`);
    if (!Array.isArray(decoded)) throw Error('unsupported_video_urls');
    let keyHex = null;
    if (decoded.some(stream => stream.is_support === true || stream.is_support === 1)) {
      const materialId = data.video_info.material_id;
      if (typeof materialId !== 'string' || !materialId) throw Error('unsupported_material');
      const keyResult = await page.executeJavaScript(pageRequest('/xe.pc_client.xe.course-bff.video.play.private.key', {app_id,user_id,material_id:materialId}));
      if (String(keyResult.payload?.code) !== '0' || !keyResult.payload?.data?.key) throw Error('playback_key_unavailable');
      keyHex = await page.executeJavaScript(`window.XiaoeTongClientSdk.decryptVideoKey(${JSON.stringify(keyResult.payload.data.key)})`);
      if (typeof keyHex !== 'string' || !/^[0-9a-f]{32}$/i.test(keyHex)) throw Error('unsupported_playback_key');
    }
    // Playback material stays in process memory / authenticated loopback only.
    return {payload:{code:0,data:{video_info:data.video_info,streams:decoded,key_hex:keyHex}}};
  }
  if (message.action === 'document') {
    const {app_id,user_id,resource_id,course_id=''} = message;
    if (!/^app[a-z0-9]+$/i.test(app_id) || !/^[a-zA-Z0-9_-]+$/.test(resource_id) || typeof user_id !== 'string') throw Error('invalid_document_identity');
    const shopToken = await shopSessionToken(page,app_id,user_id);
    const target = new URL(`https://${app_id.toLowerCase()}.h5.xiaoeknow.com/p/course/document/${resource_id}`);
    target.searchParams.set('course_id',course_id);
    target.searchParams.set('product_id',course_id);
    const fetched=await readShopPage(target,{'User-Agent':'XiaoeTongClient/1.2.18',Cookie:`ko_token=${shopToken}`});
    const detail=await shopPost(fetched.origin,'/xe.course.business.e_course.document_info.get/1.0.0',{resource_id,product_id:course_id},{'User-Agent':'XiaoeTongClient/1.2.18',Cookie:fetched.cookie});
    return authorizedDocument(detail,resource_id);

  }
  throw Error('unsupported_action');
}

function createBridge(electron, runtime) {
  fs.mkdirSync(runtime, {recursive:true});
  const token = crypto.randomBytes(32).toString('hex');
  const descriptor = path.join(runtime, 'native-bridge.json');
  let sourceTime = fs.statSync(__filename).mtimeMs;
  const server = http.createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-store');
    const supplied = Buffer.from(req.headers.authorization || '');
    const expected = Buffer.from(`Bearer ${token}`);
    if (req.headers.origin || supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) {
      res.writeHead(403); res.end('{"error":"unauthorized"}'); return;
    }
    if (req.method !== 'POST' || req.url !== '/v1') {
      res.writeHead(404); res.end('{"error":"not_found"}'); return;
    }
    let input = '', oversized = false;
    req.on('error', () => {});
    req.setEncoding('utf8');
    req.on('data', chunk => {
      input += chunk;
      if (Buffer.byteLength(input) > 65536) { oversized = true; req.destroy(); }
    });
    req.on('end', async () => {
      if (oversized) return;
      try {
        // Own bridge edits during development need no further native-client restart.
        if (process.versions.electron && fs.statSync(__filename).mtimeMs !== sourceTime) {
          delete require.cache[__filename];
          sourceTime = fs.statSync(__filename).mtimeMs;
        }
        const handler = process.versions.electron ? require(__filename).dispatch : dispatch;
        const output = await handler(electron, JSON.parse(input));
        res.end(JSON.stringify(output));
      } catch (error) {
        // Chromium exception messages may contain account tokens or signed URLs.
        const reason = error?.message === 'unsupported_request' ? 'unsupported_request' :
          error?.message === 'trial_video' ? 'trial_video' :
          /Failed to fetch/i.test(error?.message || '') ? 'fetch_failed' : 'native_request_failed';
        const errorCode = /^[A-Z_]{1,60}$/.test(error?.code || '') ? error.code : undefined;
        const detail = ['unsupported_shop_redirect','shop_session_unavailable','document_page_failed','shop_timeout','oversized_shop_response','invalid_document_identity'].includes(error?.message) ? error.message : undefined;
        res.writeHead(422); res.end(JSON.stringify({error:reason,error_type:error?.name || 'Error',error_code:errorCode,detail}));
      }
    });
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  // Optional bridge failure must never bring down the user's original client.
  server.on('error', () => {});
  server.listen(0, '127.0.0.1', () => {
    const info = {port:server.address().port,token,pid:process.pid,protocol:1};
    const temp = descriptor + '.tmp';
    try {
      fs.writeFileSync(temp, JSON.stringify(info), {mode:0o600});
      fs.renameSync(temp, descriptor);
    } catch (_) {
      server.close();
      try { fs.unlinkSync(temp); } catch (_) {}
    }
  });
  electron.app.once('before-quit', () => {
    server.close();
    try {
      if (JSON.parse(fs.readFileSync(descriptor,'utf8')).token === token) fs.unlinkSync(descriptor);
    } catch (_) {}
  });
  return server;
}

module.exports = {createBridge,dispatch,pageRequest,ENDPOINTS,authorizedDocument,shopSessionToken};
const BOOT_KEY = Symbol.for('xiaoetong.downloader.bridge.initialized');
if (process.versions.electron && !globalThis[BOOT_KEY]) {
  globalThis[BOOT_KEY] = true;
  const electron = require('electron');
  const runtime = path.resolve(__dirname, '../../runtime');
  electron.app.whenReady().then(() => createBridge(electron, runtime)).catch(() => {});
}
