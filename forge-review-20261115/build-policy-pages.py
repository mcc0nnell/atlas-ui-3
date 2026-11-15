from pathlib import Path
import html,json
root=Path('/home/commander/work/atlas-pr1175-botfix-preview-20261115/frontend')
out=Path('/home/commander/work/forge-pr1175-botfix-20261115')
bundle=(out/'policy-fixture/fixture.js').read_text()
style=list((root/'dist/assets').glob('index-*.css'))
assert len(style)==1,style
css=style[0].read_text()
scenarios={
'01-restricted-gallery':{
 'scenario':'restricted',
 'saved':'light',
 'allowed':'dark,light,windows-xp-luna',
 },
'02-revoked-theme-fallback':{
 'scenario':'revoked',
 'saved':'windows-xp-luna',
 'allowed':'dark,light',
 },
}
for name,values in scenarios.items():
 setup=(
  "window.__atlasPolicyProof="+json.dumps({'scenario':values['scenario']})+";"
  "try{localStorage.setItem('atlas-theme',"+json.dumps(values['saved'])+");"
  "localStorage.setItem('chatui-config-cache',"
  "JSON.stringify({features:{retro_themes:true,allowed_themes:['dark','light','windows-xp-luna']}}));"
  "}catch(e){window.__policyStorageError=String(e);}"
 )
 doc=('<!doctype html><html data-theme="light" data-atlas-theme="light">'
 '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
 '<script id="live-stamped-policy" data-retro-runtime="true" data-retro-themes="true"'
 ' data-allowed-themes="'+html.escape(values['allowed'])+'"></script>'
 '<style>'+css+'</style>'
 '<style>*,*:before,*:after{transition:none!important;animation:none!important}</style>'
 '</head><body><div id="fixture-root"></div>'
 '<pre id="errors" style="display:none"></pre>'
 '<script>window.onerror=function(message,source,line,col,error){'
 'document.getElementById("errors").textContent+=String(message)+(error&&error.stack||"")};'
 +setup+'</script>'
 '<script>'+bundle.replace('</script','<\\/script')+'</script></body></html>')
 p=out/'policy-pages'/f'{name}.html'
 p.write_text(doc)
 print(name,'saved',values['saved'],'allowed',values['allowed'],'html_bytes',p.stat().st_size)
