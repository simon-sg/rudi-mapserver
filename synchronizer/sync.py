#!/usr/bin/env python3
"""
Synchroniseur Rudi -> MapServer.

Lit le catalogue du noeud Rudi (API publique, GET /resources), repere les
medias FILE ouverts et non chiffres de type CSV / GeoJSON / GeoPackage,
verifie avec OGR qu'ils portent une geometrie, et (re)genere un mapfile
MapServer unique: une LAYER par media valide. Le mapfile est regenere en
entier a chaque cycle (ecriture atomique) ; aucun etat n'est conserve en base.

Voir la note d'architecture "Services SIG sur les fichiers d'un noeud Rudi"
pour le detail des choix (CSV via OGR VRT, CRS lu/devine, tolerance de clic,
feature id, extent arrondie vers l'exterieur).
"""
import csv
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.request import urlopen
from urllib.error import URLError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("rudi-mapserver-sync")

# --- Configuration (variables d'environnement) -----------------------------
CATALOG_URL = os.environ.get("CATALOG_URL", "http://host.docker.internal:3030/catalog/v1")
# URL du catalogue telle qu'un navigateur peut l'atteindre (CATALOG_URL ne
# l'est pas : host.docker.internal n'existe que depuis l'interieur des
# conteneurs) -- utilisee uniquement pour le lien de retour dans les fiches
# de metadonnees.
CATALOG_PUBLIC_URL = os.environ.get("CATALOG_PUBLIC_URL", "http://localhost:3030/catalog/v1")
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
MAPFILE_OUT = Path(os.environ.get("MAPFILE_OUT", "/etc/mapserver/generated/rudi.map"))
VRT_DIR = Path(os.environ.get("VRT_DIR", "/etc/mapserver/generated"))
MANIFEST_OUT = Path(os.environ.get("MANIFEST_OUT", "/etc/mapserver/generated/manifest.json"))
SYNC_INTERVAL_S = int(os.environ.get("SYNC_INTERVAL_S", "60"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "500"))
# Base utilisee par MapServer pour construire ses propres liens de retour
# (pagination WFS "next", xsi:schemaLocation, OnlineResource des Capabilities)
# -- doit etre une URL REELLEMENT joignable en l'etat (pas de chemin de
# "branding" fictif : voir KNOWN_ISSUES.md, le meme piege que le MetadataURL
# casse). Le "?map=RUDI&" est integre ici, pas ajoute par MapServer.
NODE_PUBLIC_URL = os.environ.get("NODE_PUBLIC_URL", "https://localhost:8444")
# OGC API Features de MapServer exige l'alias de mapfile en 1er segment du
# PATH_INFO (RFC ms-rfc-134 : /<mapfile>/ogcapi/<route>), jamais via ?map=.
OGCAPI_PUBLIC_URL = os.environ.get("OGCAPI_PUBLIC_URL", "http://localhost:8089/RUDI/ogcapi")
# Fiches de metadonnees (XML, cf. write_metadata_xml) : servies par un nginx
# statique dedie (service geo-metadata), PAS par mapserver -- son image
# camptocamp capte tout le chemin "/" via un ScriptAliasMatch global, aucun
# fichier statique n'est atteignable sur le port 8089 (voir KNOWN_ISSUES.md).
METADATA_DIR = Path(os.environ.get("METADATA_DIR", "/etc/mapserver/generated/metadata"))
METADATA_PUBLIC_URL = os.environ.get("METADATA_PUBLIC_URL", "http://localhost:8090")
# Au-dela de ce seuil, la source est convertie en FlatGeobuf (indexe
# spatialement) plutot que relue entierement par MapServer/OGR a chaque
# requete -- deroge au "zero copie" pour les quelques fichiers qui en ont
# reellement besoin (voir KNOWN_ISSUES.md). 5 Mo capture les fichiers les
# plus lourds sans toucher aux autres.
FGB_DIR = VRT_DIR / "fgb"
FLATGEOBUF_THRESHOLD_BYTES = int(os.environ.get("FLATGEOBUF_THRESHOLD_MB", "5")) * 1024 * 1024

# MIME types eligibles (cote noeud, voir rudi-catalog FileTypes.js)
GEO_MIMES = {"text/csv", "application/geo+json", "application/geopackage+sqlite3"}

X_TOKENS = {"x", "lon", "long", "longitude", "lng"}
Y_TOKENS = {"y", "lat", "latitude"}

EPSG_RANGES = [
    # (code, xmin, xmax, ymin, ymax)
    (4326, -180.0, 180.0, -90.0, 90.0),
    (2154, 0.0, 1_300_000.0, 6_000_000.0, 7_200_000.0),  # Lambert 93
    (3948, 1_000_000.0, 1_800_000.0, 7_000_000.0, 7_300_000.0),  # CC48
]


# --- Catalogue ---------------------------------------------------------------
def fetch_resources():
    """Recupere toutes les resources du catalogue (pagination offset/limit)."""
    resources = []
    offset = 0
    while True:
        url = f"{CATALOG_URL}/resources?limit={PAGE_SIZE}&offset={offset}"
        try:
            with urlopen(url, timeout=15) as r:
                data = json.load(r)
        except URLError as e:
            log.error("Catalogue inaccessible (%s): %s", url, e)
            return resources
        items = data.get("items", [])
        resources.extend(items)
        total = data.get("total", len(items))
        offset += len(items)
        if not items or offset >= total:
            break
    return resources


def iter_candidate_media(resources):
    """Produit (resource, media) pour chaque media FILE ouvert et de mime eligible.

    Deduplique par media_id : au moins une resource du catalogue liste le meme
    media plusieurs fois dans ses available_formats (constate le 2026-10-09,
    14 occurrences d'un meme media_id sur une resource -- defaut cote
    catalogue). Sans ce garde-fou, MapServer publie plusieurs LAYER de meme
    NOM, ce qui rend les Capabilities WMS invalides (noms de couche en double)."""
    seen_media_ids = set()
    for res in resources:
        for media in res.get("available_formats", []):
            if media.get("media_type") != "FILE":
                continue
            mime = media.get("file_type", "")
            if mime not in GEO_MIMES:
                continue  # inclut le rejet des mimes suffixees "+crypt" (donnees restreintes)
            if media.get("file_storage_status") != "available":
                continue
            media_id = media.get("media_id")
            if media_id in seen_media_ids:
                continue
            seen_media_ids.add(media_id)
            yield res, media


# Medias "SERVICE" : de simples liens vers une ressource externe deja en
# ligne (flux WFS/WMS tiers, fiche de metadonnees...), jamais des fichiers
# sur le volume du noeud. iter_candidate_media() les ignore totalement (ce ne
# sont pas des FILE) -- see KNOWN_ISSUES.md : on choisit de les RECENSER,
# jamais de les relayer (pas de dependance de dispo sur un service tiers,
# ni de question d'attribution/charge a assumer).
EXTERNAL_SERVICE_NAMES = {"service-wfs", "service-wms", "service-geojson"}


def collect_external_services(resources):
    """Recense les flux WFS/WMS deja references dans le catalogue (medias
    SERVICE de nom service-wfs/service-wms/service-geojson), sans les
    interroger ni les relayer -- juste leur URL d'origine et la resource
    Rudi associee, pour qu'un humain (ou un futur client) sache qu'ils
    existent a cote des couches republiees depuis les fichiers du noeud."""
    seen = set()
    services = []
    for res in resources:
        for media in res.get("available_formats", []):
            if media.get("media_type") != "SERVICE":
                continue
            if media.get("media_name") not in EXTERNAL_SERVICE_NAMES:
                continue
            media_id = media.get("media_id")
            if media_id in seen:
                continue
            seen.add(media_id)
            services.append(
                {
                    "media_id": media_id,
                    "resource_title": res.get("resource_title"),
                    "resource_id": res.get("global_id"),
                    "protocol": media.get("media_name").removeprefix("service-"),
                    "url": (media.get("connector") or {}).get("url"),
                }
            )
    return services


def locate_file(media_id, media=None):
    matches = glob.glob(str(MEDIA_DIR / "*" / f"{media_id}_*"))
    if matches:
        return Path(matches[0])
    # Volume local absent (noeud distant, ex. demo sur un noeud externe) : le media est
    # deja en open data sans authentification, donc telechargeable directement par OGR/GDAL
    # via /vsicurl/ -- ogrinfo/ogr2ogr et le VRT genere l'acceptent comme n'importe quelle
    # autre chaine de connexion, sans telechargement manuel prealable.
    url = (media or {}).get("connector", {}).get("url")
    return f"/vsicurl/{url}" if url else None


def extract_epsg_from_projection_text(text):
    if not text:
        return None
    m = re.search(r"EPSG:\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def guess_epsg_by_range(x, y):
    for code, xmin, xmax, ymin, ymax in EPSG_RANGES:
        if xmin <= x <= xmax and ymin <= y <= ymax:
            return code
    return None


def ms_escape(text):
    """Echappe un texte pour une chaine mapfile (guillemets, retours ligne)."""
    return str(text).replace('"', "'").replace("\n", " ").replace("\r", " ")


def slugify(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    name = name or "layer"
    if name[0].isdigit():
        # Un nom de couche WMS/XML doit etre un NCName valide (pas de chiffre
        # en tete) : nos media_id sont des UUID, la moitie commencent par un
        # chiffre. MapServer accepte quand meme mais le signale en warning
        # dans les Capabilities, et certains clients stricts (MapStore) le
        # rejettent silencieusement -- constate en recette le 2026-10-09.
        name = f"l_{name}"
    return name


def pad_extent(xmin, ymin, xmax, ymax, decimals=3):
    """Arrondit vers l'exterieur pour ne perdre aucun point en bordure."""
    import math

    factor = 10**decimals
    return (
        math.floor(xmin * factor) / factor,
        math.floor(ymin * factor) / factor,
        math.ceil(xmax * factor) / factor,
        math.ceil(ymax * factor) / factor,
    )


# --- Inspection OGR -----------------------------------------------------------
def ogrinfo_json(path, open_options=None):
    cmd = ["ogrinfo", "-json", "-so"]
    for opt in open_options or []:
        cmd += ["-oo", opt]
    cmd.append(str(path))
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.debug("ogrinfo KO sur %s: %s", path, e)
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def inspect_vector_file(path, resource):
    """GeoJSON / GeoPackage : lit la geometrie et le CRS directement via OGR."""
    info = ogrinfo_json(path)
    if not info or not info.get("layers"):
        return None
    layer = info["layers"][0]
    geom_fields = layer.get("geometryFields") or []
    if not geom_fields or geom_fields[0].get("type") in (None, "Unknown", "None"):
        return None  # pas de geometrie
    geom_type = geom_fields[0]["type"]
    epsg = None
    crs = geom_fields[0].get("coordinateSystem", {}).get("projjson", {})
    try:
        epsg = crs.get("id", {}).get("code")
        epsg = int(epsg) if epsg else None
    except (AttributeError, TypeError, ValueError):
        epsg = None
    if not epsg:
        epsg = extract_epsg_from_projection_text(
            (resource.get("geography") or {}).get("projection")
        )
    if not epsg:
        epsg = 4326  # convention GeoJSON/GPKG par defaut si rien d'autre n'est trouve
    extent = layer.get("geometryFields")[0].get("extent")
    return {
        "layer_name": layer["name"],
        "geom_type": geom_type,
        "epsg": epsg,
        "extent": extent,  # [xmin, ymin, xmax, ymax] ou None
        "data_path": str(path),
        "connectiontype": "OGR",
    }


def sniff_csv_geometry(path, resource):
    """CSV : detecte le separateur et des colonnes X/Y, devine le CRS."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(4096)
    except OSError:
        return None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return None
        first_row = next(reader, None)

    def tokens(col):
        norm = unicodedata.normalize("NFKD", col).encode("ascii", "ignore").decode("ascii")
        return [t for t in re.split(r"[^a-zA-Z0-9]+", norm.lower()) if t]

    x_idx = y_idx = None
    for i, col in enumerate(header):
        toks = set(tokens(col))
        if x_idx is None and toks & X_TOKENS:
            x_idx = i
        if y_idx is None and toks & Y_TOKENS:
            y_idx = i
    if x_idx is None or y_idx is None or first_row is None:
        return None  # pas de colonnes de coordonnees reconnues

    try:
        x_val = float(first_row[x_idx].replace(",", ".") if "," in first_row[x_idx] and "." not in first_row[x_idx] else first_row[x_idx])
        y_val = float(first_row[y_idx].replace(",", ".") if "," in first_row[y_idx] and "." not in first_row[y_idx] else first_row[y_idx])
    except (ValueError, IndexError):
        return None

    epsg = extract_epsg_from_projection_text((resource.get("geography") or {}).get("projection"))
    if not epsg:
        epsg = guess_epsg_by_range(x_val, y_val)
    if not epsg:
        return None  # rien ne colle : ignore (signale dans le manifeste)

    return {
        "x_col": header[x_idx],
        "y_col": header[y_idx],
        "delimiter": delimiter,
        "epsg": epsg,
        "data_path": str(path),
        "connectiontype": "OGR_VRT",
    }


def write_csv_vrt(media_id, csv_info, csv_path):
    """Ecrit un OGR VRT pour exposer un CSV comme couche ponctuelle."""
    layer_name = slugify(csv_path.stem)
    vrt_path = VRT_DIR / f"{media_id}.vrt"
    delim_name = {",": "COMMA", ";": "SEMICOLON", "\t": "TAB"}.get(csv_info["delimiter"], "COMMA")
    vrt_path.write_text(
        f"""<OGRVRTDataSource>
  <OGRVRTLayer name="{layer_name}">
    <SrcDataSource relativeToVRT="0">{csv_path}</SrcDataSource>
    <SrcLayer>{csv_path.stem}</SrcLayer>
    <GeometryType>wkbPoint</GeometryType>
    <LayerSRS>EPSG:{csv_info['epsg']}</LayerSRS>
    <GeometryField encoding="PointFromColumns" x="{csv_info['x_col']}" y="{csv_info['y_col']}"/>
    <OpenOptions>
      <OOI key="SEPARATOR">{delim_name}</OOI>
      <OOI key="AUTODETECT_TYPE">YES</OOI>
    </OpenOptions>
  </OGRVRTLayer>
</OGRVRTDataSource>
""",
        encoding="utf-8",
    )
    return vrt_path, layer_name


def write_fid_vrt(media_id, src_path, src_layer):
    """
    Enveloppe une source OGR (fichier direct ou VRT CSV deja genere) dans un
    second VRT qui materialise le FID interne OGR en une colonne reguliere
    "rudi_fid", via une requete OGR SQL passthrough. Necessaire pour OGC API
    Features (/items) qui exige un vrai champ d'identifiant ; WMS/WFS n'en ont
    pas besoin (ils generent leur propre id GML a la volee) mais reutilisent
    le meme VRT par simplicite.
    """
    layer_name = f"{slugify(str(src_layer))}_fid"
    vrt_path = VRT_DIR / f"{media_id}_fid.vrt"
    vrt_path.write_text(
        f"""<OGRVRTDataSource>
  <OGRVRTLayer name="{layer_name}">
    <SrcDataSource relativeToVRT="0">{src_path}</SrcDataSource>
    <SrcSQL dialect="OGRSQL">SELECT *, FID AS rudi_fid FROM "{src_layer}"</SrcSQL>
  </OGRVRTLayer>
</OGRVRTDataSource>
""",
        encoding="utf-8",
    )
    return vrt_path, layer_name


def materialize_flatgeobuf(media_id, src_path, src_layer_name):
    """Convertit une source volumineuse en FlatGeobuf (format GDAL avec index
    spatial integre) : MapServer peut alors filtrer par bbox sans relire tout
    le fichier a chaque requete. Traite comme un cache jetable, jamais une
    source de verite -- regenere si absent ou plus vieux que la source,
    jamais lu par rien d'autre que ce service. Retourne None (et laisse
    l'appelant retomber sur la source d'origine) si la conversion echoue."""
    FGB_DIR.mkdir(parents=True, exist_ok=True)
    fgb_path = FGB_DIR / f"{media_id}.fgb"
    try:
        src_mtime = Path(src_path).stat().st_mtime
    except OSError:
        src_mtime = 0
    if fgb_path.exists() and fgb_path.stat().st_mtime >= src_mtime:
        return fgb_path  # deja a jour
    # FlatGeobuf = un fichier mono-couche : "-overwrite" y declenche un
    # DeleteLayer() que le driver ne supporte pas ("DeleteLayer() not
    # supported by this dataset"). Il faut supprimer le fichier nous-memes.
    fgb_path.unlink(missing_ok=True)
    cmd = [
        "ogr2ogr", "-f", "FlatGeobuf",
        "-nln", src_layer_name,
        str(fgb_path), str(src_path), src_layer_name,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("Echec conversion FlatGeobuf pour %s: %s", media_id, e)
        return None
    log.info("FlatGeobuf (re)genere pour %s (%.1f Mo)", media_id, fgb_path.stat().st_size / 1_048_576)
    return fgb_path


# --- Generation du mapfile ----------------------------------------------------
LAYER_TEMPLATE = """
LAYER
  NAME "{name}"
  TYPE {ms_type}
  STATUS ON
  TEMPLATE "ms_tmpl.html"
  CONNECTIONTYPE OGR
  CONNECTION "{connection}"
  DATA "{data}"
  TOLERANCE 10
  TOLERANCEUNITS PIXELS
  PROJECTION
    "init=epsg:{epsg}"
  END
  METADATA
    "ows_title" "{title}"
    "ows_abstract" "{abstract}"
    "ows_keywordlist" "{keywords}"
    "ows_enable_request" "*"
    "gml_include_items" "all"
    "gml_featureid" "rudi_fid"
    "ows_feature_info_mime_type" "application/json"
    "ogcapi_queryables_items" "*"
    "wfs_getfeature_formatlist" "geojson,json,GML2,GML3"
    "wms_metadataurl_href" "{metadata_href}"
    "wms_metadataurl_format" "text/xml"
    "wms_metadataurl_type" "TC211"
    "wfs_metadataurl_href" "{metadata_href}"
    "wfs_metadataurl_format" "text/xml"
    "wfs_metadataurl_type" "TC211"
  END
  CLASS
    STYLE
      {style}
    END
  END
END
"""

GEOM_TO_MS_TYPE = {
    "Point": "POINT",
    "MultiPoint": "POINT",
    "LineString": "LINE",
    "MultiLineString": "LINE",
    "Polygon": "POLYGON",
    "MultiPolygon": "POLYGON",
    "3D Point": "POINT",
}

STYLE_BY_TYPE = {
    "POINT": 'SYMBOL "circle" SIZE 6 COLOR 200 50 50 OUTLINECOLOR 80 20 20',
    "LINE": "COLOR 50 100 200 WIDTH 1.5",
    "POLYGON": "COLOR 220 220 180 OUTLINECOLOR 90 90 60",
}

MAP_HEADER = """MAP
  NAME "rudi"
  STATUS ON
  EXTENT -180 -90 180 90
  UNITS dd
  SIZE 800 600
  IMAGECOLOR 255 255 255
  CONFIG "MS_ERRORFILE" "stderr"

  SYMBOL
    NAME "circle"
    TYPE ELLIPSE
    FILLED TRUE
    POINTS
      1 1
    END
  END

  OUTPUTFORMAT
    NAME "geojson"
    DRIVER "OGR/GEOJSON"
    MIMETYPE "application/geo+json"
    FORMATOPTION "FORM=SIMPLE"
    FORMATOPTION "STORAGE=memory"
  END

  OUTPUTFORMAT
    # MapStore (et d'autres clients) demandent le GetFeature WFS en
    # OUTPUTFORMAT=application/json -- le mime-type "officiel" GeoJSON est
    # "application/geo+json" (ci-dessus), "application/json" n'est accepte
    # que si on le declare separement (voir KNOWN_ISSUES.md point 9).
    NAME "json"
    DRIVER "OGR/GEOJSON"
    MIMETYPE "application/json"
    FORMATOPTION "FORM=SIMPLE"
    FORMATOPTION "STORAGE=memory"
  END

  WEB
    METADATA
      "wms_title" "Services SIG du noeud Rudi"
      "wms_onlineresource" "{base_url}/?map=RUDI&"
      "wms_srs" "EPSG:4326 EPSG:3857 EPSG:2154 EPSG:3948"
      "wms_enable_request" "*"
      "wms_allow_getmap_without_styles" "true"
      "wms_feature_info_mime_type" "application/json"
      "ows_enable_request" "*"
      "wfs_title" "Services SIG du noeud Rudi"
      "wfs_onlineresource" "{base_url}/?map=RUDI&"
      "wfs_enable_request" "*"
      "wfs_srs" "EPSG:4326 EPSG:3857 EPSG:2154 EPSG:3948"
      "wfs_getfeature_formats" "geojson,json,GML2,GML3"
      "wfs_maxfeatures" "5000"
      "ows_feature_info_mime_type" "application/json"
      "ogcapi_title" "Services SIG du noeud Rudi - OGC API Features"
      "ogcapi_root_url" "{ogcapi_url}"
    END
  END

  PROJECTION
    "init=epsg:4326"
  END
"""


def build_layer_block(name, title, ms_type, connection, data, epsg, metadata_href, abstract="", keywords=""):
    return LAYER_TEMPLATE.format(
        name=name,
        title=title,
        ms_type=ms_type,
        connection=connection,
        data=data,
        epsg=epsg,
        metadata_href=metadata_href,
        abstract=ms_escape(abstract),
        keywords=ms_escape(keywords),
        style=STYLE_BY_TYPE.get(ms_type, STYLE_BY_TYPE["POINT"]),
    )


ISO_LANG = {"fr": "fre", "en": "eng"}


def pick_lang_text(blocks, max_chars=1500):
    """Extrait le 1er texte FR (sinon le 1er disponible) d'un champ multilingue
    Rudi (synopsis/summary), tronque pour rester raisonnable dans une fiche
    de metadonnees."""
    if not blocks:
        return ""
    text = None
    for b in blocks:
        if b.get("lang") == "fr":
            text = b.get("text")
            break
    if text is None:
        text = blocks[0].get("text", "")
    text = text or ""
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    return text


def write_metadata_xml(media_id, res, media, layer_title):
    """Ecrit une fiche XML minimale (profil ISO 19139 / TC211) pour la couche,
    a partir des metadonnees deja presentes dans le catalogue Rudi (titre,
    resume, mots-cles, producteur, emprise). Remplace le <MetadataURL> par
    defaut de MapServer, qui pointe vers une URL et une operation ("GetMetadata")
    qui n'existent pas (voir KNOWN_ISSUES.md)."""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    abstract = pick_lang_text(res.get("summary")) or pick_lang_text(res.get("synopsis"))
    keywords = res.get("keywords") or []
    producer = (res.get("producer") or {}).get("organization_name", "")
    bbox = ((res.get("geography") or {}).get("bounding_box")) or {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def esc(s):
        return escape(str(s or ""))

    keyword_xml = "".join(
        f"<gmd:keyword><gco:CharacterString>{esc(k)}</gco:CharacterString></gmd:keyword>" for k in keywords
    )
    extent_xml = ""
    if bbox:
        extent_xml = f"""<gmd:extent><gmd:EX_Extent><gmd:geographicElement><gmd:EX_GeographicBoundingBox>
      <gmd:westBoundLongitude><gco:Decimal>{bbox.get('west_longitude')}</gco:Decimal></gmd:westBoundLongitude>
      <gmd:eastBoundLongitude><gco:Decimal>{bbox.get('east_longitude')}</gco:Decimal></gmd:eastBoundLongitude>
      <gmd:southBoundLatitude><gco:Decimal>{bbox.get('south_latitude')}</gco:Decimal></gmd:southBoundLatitude>
      <gmd:northBoundLatitude><gco:Decimal>{bbox.get('north_latitude')}</gco:Decimal></gmd:northBoundLatitude>
    </gmd:EX_GeographicBoundingBox></gmd:geographicElement></gmd:EX_Extent></gmd:extent>"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gco="http://www.isotc211.org/2005/gco">
  <gmd:fileIdentifier><gco:CharacterString>{esc(media_id)}</gco:CharacterString></gmd:fileIdentifier>
  <gmd:language><gco:CharacterString>fre</gco:CharacterString></gmd:language>
  <gmd:hierarchyLevel><gmd:MD_ScopeCode codeListValue="dataset">dataset</gmd:MD_ScopeCode></gmd:hierarchyLevel>
  <gmd:dateStamp><gco:Date>{now[:10]}</gco:Date></gmd:dateStamp>
  <gmd:identificationInfo>
    <gmd:MD_DataIdentification>
      <gmd:citation><gmd:CI_Citation>
        <gmd:title><gco:CharacterString>{esc(layer_title)}</gco:CharacterString></gmd:title>
      </gmd:CI_Citation></gmd:citation>
      <gmd:abstract><gco:CharacterString>{esc(abstract)}</gco:CharacterString></gmd:abstract>
      <gmd:pointOfContact><gmd:CI_ResponsibleParty>
        <gmd:organisationName><gco:CharacterString>{esc(producer)}</gco:CharacterString></gmd:organisationName>
      </gmd:CI_ResponsibleParty></gmd:pointOfContact>
      <gmd:descriptiveKeywords><gmd:MD_Keywords>{keyword_xml}</gmd:MD_Keywords></gmd:descriptiveKeywords>
      {extent_xml}
    </gmd:MD_DataIdentification>
  </gmd:identificationInfo>
  <gmd:distributionInfo><gmd:MD_Distribution>
    <gmd:transferOptions><gmd:MD_DigitalTransferOptions>
      <gmd:onLine><gmd:CI_OnlineResource>
        <gmd:linkage><gco:CharacterString>{esc(CATALOG_PUBLIC_URL)}/resources/{esc(res.get('global_id', ''))}</gco:CharacterString></gmd:linkage>
        <gmd:name><gco:CharacterString>Fiche complete dans le catalogue Rudi</gco:CharacterString></gmd:name>
      </gmd:CI_OnlineResource></gmd:onLine>
    </gmd:MD_DigitalTransferOptions></gmd:transferOptions>
  </gmd:MD_Distribution></gmd:distributionInfo>
</gmd:MD_Metadata>
"""
    (METADATA_DIR / f"{media_id}.xml").write_text(xml, encoding="utf-8")
    return f"{METADATA_PUBLIC_URL}/{media_id}.xml"


def run_cycle():
    VRT_DIR.mkdir(parents=True, exist_ok=True)
    resources = fetch_resources()
    log.info("Catalogue: %d resources recuperees", len(resources))

    layer_blocks = []
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "published": [],
        "ignored": [],
        "external_services": collect_external_services(resources),
    }

    for res, media in iter_candidate_media(resources):
        media_id = media["media_id"]
        title = ms_escape(f"{res.get('resource_title', media_id)} - {media.get('media_name', media_id)}")
        # Repris dans les Capabilities (Abstract/KeywordList) : MapStore (et
        # d'autres clients) affichent ces champs directement depuis le WMS/WFS
        # GetCapabilities, PAS depuis le <MetadataURL> -- voir KNOWN_ISSUES.md.
        abstract = pick_lang_text(res.get("summary")) or pick_lang_text(res.get("synopsis"))
        keywords = ",".join(res.get("keywords") or [])
        path = locate_file(media_id, media)
        if not path:
            manifest["ignored"].append({"media_id": media_id, "reason": "fichier_absent_du_volume"})
            continue
        is_remote = isinstance(path, str)

        mime = media["file_type"]
        try:
            if mime == "text/csv":
                if is_remote:
                    # sniff_csv_geometry lit le fichier via open() natif : pas de support
                    # /vsicurl/, hors scope pour l'instant (nos sources distantes sont des
                    # GeoJSON, pas des CSV a geometrie).
                    manifest["ignored"].append({"media_id": media_id, "name": media.get("media_name"), "reason": "csv_distant_non_supporte"})
                    continue
                csv_info = sniff_csv_geometry(path, res)
                if not csv_info:
                    manifest["ignored"].append({"media_id": media_id, "name": media.get("media_name"), "reason": "aucune_geometrie_reconnue"})
                    continue
                csv_vrt_path, csv_layer_name = write_csv_vrt(media_id, csv_info, path)
                fid_src_path, fid_src_layer, is_fgb = csv_vrt_path, csv_layer_name, False
                if path.stat().st_size > FLATGEOBUF_THRESHOLD_BYTES:
                    fgb_path = materialize_flatgeobuf(media_id, csv_vrt_path, csv_layer_name)
                    if fgb_path:
                        fid_src_path, is_fgb = fgb_path, True
                fid_vrt_path, fid_layer_name = write_fid_vrt(media_id, fid_src_path, fid_src_layer)
                metadata_href = write_metadata_xml(media_id, res, media, title)
                layer_blocks.append(
                    build_layer_block(csv_layer_name, title, "POINT", str(fid_vrt_path), fid_layer_name, csv_info["epsg"], metadata_href, abstract, keywords)
                )
                manifest["published"].append(
                    {
                        "media_id": media_id,
                        "name": media.get("media_name"),
                        "layer": csv_layer_name,
                        "epsg": csv_info["epsg"],
                        "geom_type": "Point",
                        "resource_id": res.get("global_id"),
                        "resource_local_id": res.get("local_id"),
                        "resource_title": res.get("resource_title"),
                        "flatgeobuf": is_fgb,
                    }
                )
            else:
                vinfo = inspect_vector_file(path, res)
                if not vinfo:
                    manifest["ignored"].append({"media_id": media_id, "name": media.get("media_name"), "reason": "aucune_geometrie_reconnue"})
                    continue
                ms_type = GEOM_TO_MS_TYPE.get(vinfo["geom_type"])
                if not ms_type:
                    manifest["ignored"].append({"media_id": media_id, "name": media.get("media_name"), "reason": f"type_geometrie_non_gere:{vinfo['geom_type']}"})
                    continue
                layer_name = slugify(Path(media["media_name"]).stem if is_remote else path.stem)
                fid_src_path, fid_src_layer, is_fgb = vinfo["data_path"], vinfo["layer_name"], False
                # Taille locale inconnue pour une source distante (/vsicurl/) : pas de bascule
                # FlatGeobuf dans ce cas, nos sources distantes restent petites pour l'instant.
                if not is_remote and path.stat().st_size > FLATGEOBUF_THRESHOLD_BYTES:
                    fgb_path = materialize_flatgeobuf(media_id, vinfo["data_path"], vinfo["layer_name"])
                    if fgb_path:
                        fid_src_path, is_fgb = fgb_path, True
                fid_vrt_path, fid_layer_name = write_fid_vrt(media_id, fid_src_path, fid_src_layer)
                metadata_href = write_metadata_xml(media_id, res, media, title)
                layer_blocks.append(
                    build_layer_block(layer_name, title, ms_type, str(fid_vrt_path), fid_layer_name, vinfo["epsg"], metadata_href, abstract, keywords)
                )
                manifest["published"].append(
                    {
                        "media_id": media_id,
                        "name": media.get("media_name"),
                        "layer": layer_name,
                        "epsg": vinfo["epsg"],
                        "geom_type": vinfo["geom_type"],
                        "resource_id": res.get("global_id"),
                        "resource_local_id": res.get("local_id"),
                        "resource_title": res.get("resource_title"),
                        "flatgeobuf": is_fgb,
                    }
                )
        except Exception as e:  # un fichier corrompu/imprevu ne doit pas arreter le cycle
            log.warning("Echec sur media %s (%s): %s", media_id, path, e)
            manifest["ignored"].append({"media_id": media_id, "name": media.get("media_name"), "reason": f"erreur:{e}"})

    # Regroupe les couches publiees par ressource (une resource peut avoir
    # plusieurs fichiers donc plusieurs couches) : sert de base a
    # declare_services.py pour savoir a quelles resources ajouter un media
    # SERVICE pointant vers nos propres flux WMS/WFS/OGC API.
    by_resource = {}
    for p in manifest["published"]:
        rid = p["resource_id"]
        if rid not in by_resource:
            by_resource[rid] = {
                "resource_id": rid,
                "resource_local_id": p["resource_local_id"],
                "resource_title": p["resource_title"],
                "layers": [],
            }
        by_resource[rid]["layers"].append(p["layer"])
    manifest["resources_with_layers"] = list(by_resource.values())

    header = MAP_HEADER.format(base_url=NODE_PUBLIC_URL, ogcapi_url=OGCAPI_PUBLIC_URL)
    content = header + "\n".join(layer_blocks) + "\nEND\n"

    tmp_path = MAPFILE_OUT.with_suffix(".map.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(MAPFILE_OUT)

    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False)
    MANIFEST_OUT.write_text(manifest_json, encoding="utf-8")
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    # Copie servie par geo-metadata (port 8090) : le manifest.json "racine"
    # n'est atteignable par aucun serveur HTTP (voir KNOWN_ISSUES.md point 5
    # sur l'image MapServer qui capte tout "/"), seul generated/metadata/
    # l'est.
    (METADATA_DIR / "manifest.json").write_text(manifest_json, encoding="utf-8")

    log.info(
        "Cycle termine: %d couches publiees, %d medias ignores -> %s",
        len(manifest["published"]),
        len(manifest["ignored"]),
        MAPFILE_OUT,
    )
    return manifest


# Declaration des medias SERVICE autonome : apres chaque cycle ou le jeu de
# couches publiees a change, geo-sync appelle lui-meme declare_services.py
# (selon les memes variables d'environnement que le reste du cycle) pour que
# `docker compose up` suffise, sans pipeline externe. Idempotent cote
# declare_services.py, et jamais bloquant pour la boucle de sync.
SELF_DECLARE = os.environ.get("RUDI_GEO_SELF_DECLARE", "true").lower() not in ("0", "false", "no")


def self_declare():
    try:
        result = subprocess.run(
            [sys.executable, "/app/declare_services.py", "--apply"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            log.warning("declare_services a echoue (code %s): %s", result.returncode, result.stderr.strip())
    except Exception as e:
        log.warning("Echec de l'appel a declare_services.py: %s", e)


def main():
    once = "--once" in sys.argv
    last_hash = None
    while True:
        try:
            manifest = run_cycle()
            # Ne redeclare que si le jeu "resources_with_layers" a change
            # (hash du contenu trie) : evite de spammer l'API manager toutes
            # les 60s sur un catalogue inchange.
            if SELF_DECLARE and manifest is not None:
                current_hash = hashlib.sha256(
                    json.dumps(manifest["resources_with_layers"], sort_keys=True).encode()
                ).hexdigest()
                if current_hash != last_hash:
                    self_declare()
                    last_hash = current_hash
        except Exception:
            log.exception("Echec du cycle de synchronisation")
        if once:
            break
        time.sleep(SYNC_INTERVAL_S)


if __name__ == "__main__":
    main()
