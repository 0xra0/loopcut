#!/usr/bin/env python3
"""Print ComfyUI history output shapes and paths for a known prompt id."""
import argparse, json, urllib.request
p=argparse.ArgumentParser()
p.add_argument('prompt_id')
p.add_argument('--url',default='http://127.0.0.1:8188')
a=p.parse_args()
with urllib.request.urlopen(f'{a.url}/history/{a.prompt_id}',timeout=10) as res:
    data=json.load(res)
h=data.get(a.prompt_id, {})
print('status:',h.get('status',{}).get('status_str'))
for node_id, output in h.get('outputs',{}).items():
    print('node',node_id,'keys',list(output))
    print(json.dumps(output,indent=2,default=str)[:3000])
