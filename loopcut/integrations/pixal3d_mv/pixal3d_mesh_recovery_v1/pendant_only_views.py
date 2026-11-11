#!/usr/bin/env python3
"""Optional: create independently named 4-view inputs emphasizing pendant instead of chain.

Input: transparent PNGs from `pa.py split`. Outputs new 1024x1024 transparent
PNGs suitable for `pa.py mesh <new_name>`; does not edit the original views.
The crop is deliberately conservative: manually inspect all 4 outputs.
"""
import argparse
from pathlib import Path
import numpy as np
from PIL import Image

NAMES = ('front','left','back','right')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', type=Path, help='ComfyUI input/medallion_necklace_views')
    ap.add_argument('dst', type=Path, help='ComfyUI input/medallion_pendant_views')
    ap.add_argument('--top', type=float, default=.34, help='cut above this fraction of original canvas')
    ap.add_argument('--bottom', type=float, default=.94, help='crop below this fraction of original canvas')
    a=ap.parse_args()
    if a.src.resolve()==a.dst.resolve():
        ap.error('Output directory must differ from input directory')
    if not (0 < a.top < a.bottom <= 1):
        ap.error('Need 0 < --top < --bottom <= 1')
    a.dst.mkdir(parents=True, exist_ok=True)
    for view in NAMES:
        srcfile=a.src/f'{view}.png'
        im=Image.open(srcfile).convert('RGBA')
        w,h=im.size
        pix=np.array(im)
        y0=int(h*a.top); y1=int(h*a.bottom)
        # Estimate center from pendant region, ignoring chain higher up.
        ycore0=min(y1-1,y0+int(.18*(y1-y0)))
        yy,xx=np.nonzero(pix[ycore0:y1,:,3]>64)
        if len(xx)<30:
            raise ValueError(f'{srcfile}: too few solid alpha pixels; examine split masks')
        center=float(np.median(xx))
        # Remove all content above crop (chain), and taper the horizontal
        # acceptance band near the bail so distant diagonal links disappear.
        section=pix[y0:y1].copy()
        length=y1-y0
        bandh=max(1,int(length*.16))
        xxgrid=np.arange(w)
        for j in range(bandh):
            width=length*(.08 + .34*j/bandh)
            section[j,(np.abs(xxgrid-center)>width),3]=0
        # Preserve one scale for all four views, so front and side remain aligned.
        x0=round(center-length/2)
        canvas=Image.new('RGBA',(length,length),(0,0,0,0))
        source=Image.fromarray(section,'RGBA')
        canvas.paste(source,(-x0,0))
        result=canvas.resize((1024,1024),Image.Resampling.LANCZOS)
        target=a.dst/f'{view}.png'
        result.save(target)
        print(f'{view}: center_x={center:.1f}, crop_y=({y0},{y1}), saved {target}')
    print('Review the four output PNGs before calling pa.py mesh!')


if __name__=='__main__':
    main()
