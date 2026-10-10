#!/usr/bin/env python3
"""
Recette automatique de la brique SIG du noeud Rudi.

Simule les requetes des clients identifies dans la note d'architecture :
- Portail Rudi (carte)  : WFS GetFeature, GeoJSON, EPSG:3857
- MapStore               : WMS GetCapabilities, GetMap, GetFeatureInfo
- Datahub                : OGC API Features (collections / items), servi
                           nativement par MapServer -- voir README.md
                           "Endpoints" : l'alias de mapfile doit etre le 1er
                           segment du chemin (/RUDI/ogcapi/...), jamais un
                           ?map= classique (RFC ms-rfc-134 de MapServer).

Usage: python3 test_services.py [base_url]  (defaut: http://localhost:8089)
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8089"

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    mark = "OK " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" - {detail}" if detail and not ok else ""))


def get(path):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read()


# 1. Decouverte : au moins une couche publiee
status, body = get("/?map=RUDI&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities")
check("WMS GetCapabilities repond 200", status == 200, f"status={status}")
n_layers = body.count(b"<Layer ")
check("WMS GetCapabilities liste des couches", n_layers > 1, f"{n_layers} <Layer> trouves")

# Prend la premiere couche polygone/point connue pour le reste des tests
import re

m = re.search(rb"<Name>([a-z0-9_]+canton)</Name>", body)
layer = m.group(1).decode() if m else None
if not layer:
    m = re.search(rb"<Layer queryable=\"1\"[^>]*>\s*<Name>([a-z0-9_]+)</Name>", body)
    layer = m.group(1).decode() if m else None
check("Une couche de test identifiee", layer is not None, layer or "aucune")

if layer:
    # 2. MapStore : GetMap
    status, body = get(
        f"/?map=RUDI&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={layer}"
        "&CRS=EPSG:4326&BBOX=47.0,-2.5,48.5,-1.0&WIDTH=256&HEIGHT=256&FORMAT=image/png"
    )
    check("MapStore: WMS GetMap renvoie un PNG", status == 200 and body[:4] == b"\x89PNG", f"status={status}")

    # 3. MapStore : GetFeatureInfo (GML - cf. KNOWN_ISSUES.md pour le JSON)
    status, body = get(
        f"/?map=RUDI&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetFeatureInfo&LAYERS={layer}"
        f"&QUERY_LAYERS={layer}&CRS=EPSG:4326&BBOX=47.0,-2.5,48.5,-1.0&WIDTH=256&HEIGHT=256"
        "&I=128&J=128&INFO_FORMAT=application/vnd.ogc.gml&FEATURE_COUNT=1"
    )
    check(
        "MapStore: WMS GetFeatureInfo (GML) renvoie des attributs",
        status == 200 and b"<gml:" in body,
        f"status={status}",
    )

    # 4. Portail Rudi : WFS GetFeature en GeoJSON, EPSG:3857
    status, body = get(
        f"/?map=RUDI&SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES={layer}"
        "&OUTPUTFORMAT=geojson&SRSNAME=EPSG:3857&COUNT=3"
    )
    ok = False
    if status == 200:
        try:
            data = json.loads(body)
            ok = data.get("type") == "FeatureCollection" and len(data.get("features", [])) > 0
        except json.JSONDecodeError:
            ok = False
    check("Portail Rudi: WFS GetFeature GeoJSON (EPSG:3857)", ok, f"status={status}")

    # 5. Datahub : OGC API Features, servi nativement par MapServer
    #    (alias "RUDI" en 1er segment du PATH_INFO, pas de ?map=)
    status, body = get("/RUDI/ogcapi/collections")
    ok = False
    if status == 200:
        try:
            ok = len(json.loads(body).get("collections", [])) > 0
        except json.JSONDecodeError:
            ok = False
    check("Datahub: OGC API Features /RUDI/ogcapi/collections", ok, f"status={status}")

    status, body = get(f"/RUDI/ogcapi/collections/{layer}/items?limit=1")
    ok = False
    if status == 200:
        try:
            data = json.loads(body)
            ok = len(data.get("features", [])) > 0
        except json.JSONDecodeError:
            ok = False
    check("Datahub: OGC API Features .../items", ok, f"status={status}")

print()
n_ok = sum(1 for _, ok, _ in results if ok)
print(f"{n_ok}/{len(results)} controles passes")
sys.exit(0 if n_ok == len(results) else 1)
