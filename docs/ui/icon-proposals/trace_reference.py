"""Trace the user-selected raster reference without redesigning its silhouette."""
from pathlib import Path
from PIL import Image
import numpy as np
from scipy import ndimage

root = Path(__file__).parent
im = np.asarray(Image.open(root / 'theme-flat-b.png').convert('RGB')).astype(float)
h, w = im.shape[:2]
y, x = np.indices((h, w))
palette = np.array([[239,234,246], [138,117,173], [74,62,91], [177,153,209],
                    [246,241,247], [218,207,235], [192,173,219]])
classes = ((im[:,:,None,:] - palette[None,None,:,:]) ** 2).sum(3).argmin(2)

def component(mask, point):
    labels, _ = ndimage.label(mask)
    label = labels[point[1], point[0]]
    assert label
    return ndimage.binary_fill_holes(labels == label)

tile = component((im[:,:,2]-im[:,:,1] > 5) & (x>30) & (x<1220) & (y>15) & (y<1210), (200,200))
roof = component(classes == 1, (627,250))
bottom = component(classes == 1, (627,960))
left = component(classes == 2, (300,600))
right = component(classes == 2, (950,600))
ring = component(classes == 3, (460,600))
cube = component(((classes == 4) | (classes == 5) | (classes == 6) | (classes == 1))
                 & (x>480) & (x<774) & (y>440) & (y<804), (625,540))

def polygon(mask):
    edges = {}
    top = mask & ~np.pad(mask[:-1], ((1,0),(0,0)))
    bottom = mask & ~np.pad(mask[1:], ((0,1),(0,0)))
    left = mask & ~np.pad(mask[:,:-1], ((0,0),(1,0)))
    right = mask & ~np.pad(mask[:,1:], ((0,0),(0,1)))
    for yy,xx in zip(*np.where(top)): edges[(int(xx),int(yy))] = (int(xx+1),int(yy))
    for yy,xx in zip(*np.where(right)): edges[(int(xx+1),int(yy))] = (int(xx+1),int(yy+1))
    for yy,xx in zip(*np.where(bottom)): edges[(int(xx+1),int(yy+1))] = (int(xx),int(yy+1))
    for yy,xx in zip(*np.where(left)): edges[(int(xx),int(yy+1))] = (int(xx),int(yy))
    loops = []
    while edges:
        start = next(iter(edges)); point = start; out = []
        while point in edges:
            out.append(point); point = edges.pop(point)
            if point == start: break
        if len(out) > 20: loops.append(out)
    return np.array(max(loops, key=len), dtype=float)

def simplify(points, epsilon=.7):
    if len(points) < 3: return points
    a,b = points[0],points[-1]; v=b-a
    distance = np.abs(v[0]*(a[1]-points[:,1])-(a[0]-points[:,0])*v[1])/max(np.linalg.norm(v),1e-9)
    i = int(distance.argmax())
    if distance[i] > epsilon:
        return np.concatenate([simplify(points[:i+1])[:-1], simplify(points[i:])])
    return np.array([a,b])

def path(mask):
    points = polygon(mask)
    split = int(np.linalg.norm(points-points[0],axis=1).argmax())
    points = np.concatenate([simplify(points[:split+1])[:-1],
                            simplify(np.concatenate([points[split:],points[:1]]))[:-1]])
    incoming=[]; outgoing=[]
    for i,p in enumerate(points):
        a=p-points[i-1]; b=points[(i+1)%len(points)]-p
        incoming.append(p-a*min(.35/max(np.linalg.norm(a),1),.2))
        outgoing.append(p+b*min(.35/max(np.linalg.norm(b),1),.2))
    xy = lambda p: f'{p[0]:.2f},{p[1]:.2f}'
    result='M'+xy(incoming[0])
    for i,p in enumerate(points):
        if i: result+='L'+xy(incoming[i])
        result+='Q'+xy(p)+' '+xy(outgoing[i])
    return result+'Z'

parts=[]
for name,mask,color in [('tile',tile,'#EFEAF6'), ('top',roof,'#8975AC'),
                        ('bottom',bottom,'#8975AC'), ('left',left,'#4A3E5B'),
                        ('right',right,'#4A3E5B'), ('core-shell',ring,'#B199D1')]:
    coordinates=np.argwhere(mask); lo=coordinates.min(0); hi=coordinates.max(0)
    print(name, [int(lo[1]),int(lo[0]),int(hi[1]+1),int(hi[0]+1)])
    parts.append(f'<path id="{name}" d="{path(mask)}" fill="{color}"/>')
parts.append(f'<defs><clipPath id="cube-clip"><path d="{path(cube)}"/></clipPath></defs>')
parts.append('<g clip-path="url(#cube-clip)"><path d="M480 529L627 444L774 529L627 620Z" fill="#F6F1F7"/><path d="M480 529L627 620V807L480 716Z" fill="#DACFEB"/><path d="M774 529L627 620V807L774 716Z" fill="#C0ADDB"/><path d="M486 533L620 616Q627 620 634 616L769 533M627 621V797" fill="none" stroke="#8975AC" stroke-width="2.2" stroke-opacity=".8" stroke-linecap="round" stroke-linejoin="round"/></g>')
svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1254 1254"><title>CodexHub — reference-aligned SVG</title>'+''.join(parts)+'</svg>'
(root / 'reference-aligned-b.svg').write_text(svg)
