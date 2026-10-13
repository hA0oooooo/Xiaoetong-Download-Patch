const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {once} = require('node:events');
const {createBridge,dispatch} = require('../src/xiaoetong_assistant/native_bridge.cjs');

function mockElectron(payload = {code:0,data:{list:[],is_last:true}}, sdkOverrides = {}) {
  const requests = [];
  const page = {
    getURL:()=> 'xiaoetongclient://entrance/index.html',
    executeJavaScript: async source => vm.runInNewContext(source, {
      window:{XiaoeTongClientSdk:{getAppToken:async()=> 'session-private',deobfuscateString:async value=>value,...sdkOverrides}},
      AbortSignal, fetch:async (url,options)=>{
        requests.push({url,options});
        return {ok:true,json:async()=>typeof payload === 'function' ? payload(url, options) : payload};
      },
    }),
  };
  return {requests,app:{once:()=>{}},BrowserWindow:{getAllWindows:()=>[{isDestroyed:()=>false,webContents:page}]}};
}

test('uses native session headers without returning the token', async()=>{
  const electron = mockElectron();
  const result = await dispatch(electron,{action:'post',endpoint:'/xe.pc_client.course/my.all.course.lists.get/3.0.1',body:{page:1}});
  assert.equal(electron.requests[0].options.headers['App-Token'],'session-private');
  assert.equal(electron.requests[0].options.redirect,'error');
  assert.equal(electron.requests[0].options.credentials,'omit');
  assert.equal(JSON.parse(electron.requests[0].options.body).platform,'pc_client');
  assert.equal(JSON.stringify(result).includes('session-private'),false);
});

test('reads the current login on each request and recognizes account changes',async()=>{
  let currentToken='session-a';
  const electron=mockElectron((_url,options)=>({code:0,data:{
    list:[{user_id:options.headers['App-Token']==='session-a'?'account-a':'account-b'}],is_last:true,
  }}),{getAppToken:async()=>currentToken});
  const request={action:'post',endpoint:'/xe.pc_client.course/my.all.course.lists.get/3.0.1',body:{page:1}};
  assert.equal((await dispatch(electron,request)).payload.data.list[0].user_id,'account-a');
  currentToken='session-b';
  const second=await dispatch(electron,request);
  assert.equal(second.payload.data.list[0].user_id,'account-b');
  assert.equal(JSON.stringify(second).includes('session-b'),false);
  currentToken='';
  assert.equal((await dispatch(electron,{action:'status'})).authenticated,false);
  const loggedOut=await dispatch(electron,request);
  assert.equal(loggedOut.error,'login_required');
  assert.equal(electron.requests.length,2);
});

test('rejects non-list/write endpoints before issuing a fetch',async()=>{
  const electron = mockElectron();
  await assert.rejects(dispatch(electron,{action:'post',endpoint:'/account/delete',body:{}}));
  assert.equal(electron.requests.length,0);
});

test('sidebar materials require course access and preserve preview-only restrictions',async()=>{
  const rows=[
    {material_id:'pdf',title:'Notes.pdf',type:9,operate_type:1,url:'https://example.invalid/notes.pdf'},
    {material_id:'preview',title:'Preview.pdf',type:9,operate_type:2,url:'https://example.invalid/preview.pdf'},
    {material_id:'slides',title:'Slides.pptx',type:7,operate_type:1,url:'https://example.invalid/slides.pptx'},
  ];
  const request={action:'courseware',app_id:'a',user_id:'u',course_id:'c'};
  const electron=mockElectron(url=>url.includes('resource_catalog_list')?{code:0,data:{available:1,subscribe:1}}:{code:0,data:rows});
  const result=await dispatch(electron,request);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.data.list)),[
    {material_id:'pdf',title:'Notes.pdf',type:9,file_url:'https://example.invalid/notes.pdf'},
  ]);
  assert.equal(electron.requests.length,2);
  for(const flags of [{available:0,subscribe:1},{available:1,subscribe:0},{available:1,subscribe:1,is_ban:1},{available:1,subscribe:1,is_try:1}]) {
    const denied=mockElectron({code:0,data:flags});
    assert.equal((await dispatch(denied,request)).payload.code,'courseware_access_denied');
    assert.equal(denied.requests.length,1);
  }
});

test('resolves normal authorized video metadata through the native SDK',async()=>{
  const streams = [{url:'https://example.invalid/video.mp4',is_support:0}];
  const electron = mockElectron({code:0,data:{video_info:{material_id:'m'},video_urls:JSON.stringify(streams)}});
  const result = await dispatch(electron,{action:'video',app_id:'a',user_id:'u',resource_id:'v'});
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.data.streams)),streams);
});

test('does not resolve a trial response as a full video',async()=>{
  const electron = mockElectron({code:0,data:{is_try:1,video_info:{},video_urls:'[]'}});
  await assert.rejects(dispatch(electron,{action:'video',app_id:'a',user_id:'u',resource_id:'v'}));
});

test('resolves playback key only for the material returned by authorized detail',async()=>{
  const streams = [{url:'https://example.invalid/video.m3u8',is_support:true}];
  const electron = mockElectron((url,options)=>{
    if (url.endsWith('/xe.pc_client.xe.course-bff.video.play.private.key')) {
      assert.equal(JSON.parse(options.body).material_id,'authorized-material');
      return {code:0,data:{key:'encoded-playback-key'}};
    }
    return {code:0,data:{video_info:{material_id:'authorized-material'},video_urls:JSON.stringify(streams)}};
  },{decryptVideoKey:async value=>{
    assert.equal(value,'encoded-playback-key');
    return '0123456789abcdef0123456789abcdef';
  }});
  const result = await dispatch(electron,{action:'video',app_id:'a',user_id:'u',resource_id:'v',material_id:'untrusted-material'});
  assert.equal(result.payload.data.key_hex,'0123456789abcdef0123456789abcdef');
  assert.equal(electron.requests.length,2);
  assert.equal(JSON.stringify(result).includes('session-private'),false);
});

test('loopback server requires its private token and rejects browser origins',async()=>{
  const runtime = fs.mkdtempSync(path.join(os.tmpdir(),'xet-bridge-test-'));
  const server = createBridge(mockElectron(),runtime);
  try {
    await once(server,'listening');
    const descriptor = JSON.parse(fs.readFileSync(path.join(runtime,'native-bridge.json')));
    assert.equal(server.address().address,'127.0.0.1');
    const url = `http://127.0.0.1:${descriptor.port}/v1`;
    const options = {method:'POST',headers:{Authorization:`Bearer ${descriptor.token}`},body:JSON.stringify({action:'status'})};
    assert.equal((await fetch(url,{...options,headers:{}})).status,403);
    assert.equal((await fetch(url,{...options,headers:{...options.headers,Origin:'https://example.invalid'}})).status,403);
    const response = await fetch(url,options);
    assert.equal(response.status,200);
    assert.deepEqual(await response.json(),{sdk:true,authenticated:true});
  } finally {
    await new Promise(resolve=>server.close(resolve));
    fs.rmSync(runtime,{recursive:true,force:true});
  }
});


test('document access, bans and identity mismatches remain enforced',()=>{
  const {authorizedDocument}=require('../src/xiaoetong_assistant/native_bridge.cjs');
  const data={resource_id:'document',resource_type:51,is_subscribe:1,is_ban:0,file_url:'https://example.invalid/file.pdf',file_name:'file.pdf',is_download:0};
  assert.equal(authorizedDocument({code:0,data},'document').payload.data.file_name,'file.pdf');
  assert.equal(authorizedDocument({code:403002},'document').payload.code,403002);
  for(const flag of [{is_subscribe:0},{is_ban:1},{is_try:1}]) {
    assert.equal(authorizedDocument({code:0,data:{...data,...flag}},'document').payload.code,'document_access_denied');
  }
  assert.throws(()=>authorizedDocument({code:0,data},'different'));
});

test('generic bridge does not export shop session tokens',async()=>{
  const electron=mockElectron();
  await assert.rejects(dispatch(electron,{action:'post',endpoint:'/xe.pc_client.course/kotoken.create/1.0.0',body:{}}));
  assert.equal(electron.requests.length,0);
});


test('shop session reuse is checked against the current native session',async()=>{
  const {shopSessionToken}=require('../src/xiaoetong_assistant/native_bridge.cjs');
  const electron=mockElectron((url)=>url.includes('kotoken.check')?{code:0,data:{is_valid:1}}:{code:0,data:{token:{value:'shop-only-secret',expires:60}}});
  const page=electron.BrowserWindow.getAllWindows()[0].webContents;
  assert.equal(await shopSessionToken(page,'appFixture','userFixture'),'shop-only-secret');
  assert.equal(await shopSessionToken(page,'appFixture','userFixture'),'shop-only-secret');
  assert.equal(electron.requests.length,2);
  assert.ok(electron.requests[1].url.includes('kotoken.check'));
});
