#!/usr/bin/env python3
"""
Declare, sur le catalogue d'un noeud Rudi, un media SERVICE par protocole
(WMS, WFS, OGC API Features) pour chaque resource qui a au moins une couche
publiee par rudi-mapserver -- pour que le catalogue/portail Rudi affiche
"cette ressource a aussi un flux WMS/WFS disponible ici", comme il le fait
deja pour les flux externes (data.geopf.fr...).

Lit generated/manifest.json (cle "resources_with_layers", ecrite par
synchronizer/sync.py) ; n'interroge jamais le catalogue pour decouvrir les
couches -- la synchronisation mapfile et cette declaration sont deux etapes
separees et independamment relancables.

Utilise la librairie rudi_node_write (API privee du noeud producteur, cote
manager). Deux usages coexistent, independants et sans conflit (le script
etant idempotent) :

  - dans le conteneur geo-sync, qui l'appelle lui-meme apres chaque cycle du
    synchroniseur quand le jeu de couches publiees change (voir
    synchronizer/sync.py::self_declare) -- c'est ce qui permet a un simple
    `docker compose up` de tout declarer, sans pipeline externe ;
  - cote hote, pour un humain ou un pipeline externe de publication, avec la
    librairie installee localement :

    pip install --user rudi-node-write
    python3 declare_services.py            # dry-run : affiche ce qui serait fait
    python3 declare_services.py --apply    # ecrit reellement sur le noeud

Idempotent : si un media de meme media_name existe deja sur une resource
avec la meme URL, il n'est pas touche ; si l'URL a change, le media
existant est mis a jour (meme media_id) plutot que duplique.
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    from rudi_node_write.rudi_node_writer import RudiNodeWriter
    from rudi_node_write.connectors.rudi_node_auth import RudiNodeAuth
    from rudi_node_write.rudi_types.rudi_meta import RudiMetadata
except ImportError:
    print("rudi_node_write n'est pas installe : pip install --user rudi-node-write", file=sys.stderr)
    sys.exit(1)


def _put_metadata(writer, rudi_metadata: dict) -> None:
    """Equivalent de writer.put_metadata(rudi_metadata), sans son bug de serialisation :
    to_json() (utilise en interne par put_metadata) perd ou corrompt les champs liste de
    Serializable (summary/synopsis) -- clean_nones() les ramene a des list Python nues
    avant le test isinstance(val, Serializable) du bloc liste, qui porte alors sur la
    liste elle-meme (toujours fausse) au lieu de chaque element : les objets
    RudiDictionaryEntry ressortent tels quels, non convertis en dict -- corps de requete
    illisible cote noeud/portail (confirme en conditions reelles : PUT refuse avec une
    erreur technique generique des qu'un summary/synopsis est present). to_json_str()
    evite ca (json.dumps(default=...), qui convertit chaque objet individuellement).

    RudiMetadata ne modelise par ailleurs que lang/text pour summary[] : le round-trip
    from_json()/to_json_str() perd silencieusement un eventuel summary[].html deja present
    (resume riche, voir pivot de moissonneuse-batteuse) -- reinjecte ici, apparie par lang."""
    meta_obj = RudiMetadata.from_json(rudi_metadata)
    meta_json = json.loads(meta_obj.to_json_str(ensure_ascii=True))
    htmls = {e.get("lang"): e["html"] for e in rudi_metadata.get("summary", []) if e.get("html")}
    for entry in meta_json.get("summary", []):
        html = htmls.get(entry.get("lang"))
        if html:
            entry["html"] = html
    writer.connector.put_catalog("resources", meta_json)

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", Path(__file__).parent / "generated" / "manifest.json"))
MANAGER_URL = os.environ.get("RUDI_MANAGER_URL", "http://localhost:3032/manager")
RUDI_USER = os.environ.get("RUDI_USER", "admin")
RUDI_PASSWORD = os.environ.get("RUDI_PASSWORD", "admin")

# URLs de nos propres flux, telles qu'ecrites dans connector.url du catalogue.
# "localhost", PAS "host.docker.internal" : contrairement a ce qu'on a cru un
# temps (piege 12b de PROCEDURE_PATCH_IMAGE_PORTAIL_RUDI.md, qui concerne en
# realite un AUTRE champ -- apigateway_data.api.url, interne a apigateway,
# jamais ce que le catalogue valide), le portail valide connector.url avec
# Apache Commons UrlValidator(ALLOW_LOCAL_URLS) (rudi-portal-source,
# ConnectorValidator.java) : "localhost" (hote simple) passe, mais
# "host.docker.internal" echoue (ERR-307 "ne correspond pas au format
# attendu") -- son pseudo-TLD ".internal" n'est pas reconnu, meme avec
# ALLOW_LOCAL_URLS. Confirme empiriquement sur ce meme portail. En
# production, ce sera de toute facon l'URL publique reelle du noeud, jamais
# "localhost" ni "host.docker.internal" -- cette distinction n'existe que
# dans cet environnement de dev local. Voir KNOWN_ISSUES.md.
#
# Port 8444, PAS 8443 : apigateway ecoute lui-meme sur 8443 en interne (son
# propre serveur Reactor Netty HTTPS). Quand apigateway relaie une requete
# /medias/.../wms vers connector.url, "localhost" DEPUIS SON PROPRE
# CONTENEUR (reseau bridge rudiplatform_default, jamais host network) ne
# resout JAMAIS vers l'hote ni vers mapserver-tls -- seulement vers
# apigateway lui-meme. Avec le port 8443 (identique), la requete bouclait
# silencieusement sur l'app apigateway elle-meme (chemin "/" non reconnu ->
# redirection /login, confondu un temps avec un probleme d'auth -- voir
# KNOWN_ISSUES.md). Le port 8444 ne resout pas ce probleme de fond (le
# relais via le portail reste non fonctionnel en local, "localhost" n'etant
# jamais joignable depuis un autre conteneur), mais evite au moins le faux
# symptome : la connexion est refusee proprement plutot que redirigee vers
# /login. En production, le noeud a un domaine public reel, sans cette
# ambiguite.
WMS_URL = os.environ.get("RUDI_GEO_WMS_URL", "https://localhost:8444/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities")
WFS_URL = os.environ.get("RUDI_GEO_WFS_URL", "https://localhost:8444/?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetCapabilities")
OGCAPI_COLLECTIONS_URL = os.environ.get("RUDI_GEO_OGCAPI_URL", "https://localhost:8444/RUDI/ogcapi/collections")

# PAS "page" : le champ n'est pas valide cote noeud (simple String, aucune
# contrainte -- voir le commentaire "TODO: define this properly" dans
# rudi-catalog/src/definitions/models/Media.js), mais le PORTAIL (service
# kalim) valide "interface_contract" contre un enum Java strict qui ne
# contient PAS "page". N'y faire confiance qu'aux valeurs testees et
# confirmees reellement acceptees : "wfs" fonctionne (le service kalim
# renvoie "Handle Integration request treatment KO. java.lang.
# IllegalArgumentException: Unexpected code '<valeur>'" pour toute valeur
# non reconnue -- confirme en pratique pour "page" ET "gdata", malgre les
# deux etant presentees comme valides par des sources tierces). "wms" en
# deduction (meme famille que wfs, non teste exception-par-exception mais
# jamais vu en erreur). Pour OGC API Features, qui n'a pas d'entree dediee
# connue, reutilisation de "wfs" (meme nature : acces aux entites) plutot
# qu'une nouvelle valeur non verifiee -- voir KNOWN_ISSUES.md.
PROTOCOLS = {
    "wms": {"interface_contract": "wms", "caption": "Flux WMS du noeud (rudi-mapserver)"},
    "wfs": {"interface_contract": "wfs", "caption": "Flux WFS du noeud (rudi-mapserver)"},
    "ogcapi": {"interface_contract": "wfs", "caption": "Flux OGC API Features du noeud (rudi-mapserver)"},
}

# Prefixe commun a tous les media_name qu'on gere ici -- sert a reperer, parmi
# les medias SERVICE existants d'une resource, ceux qu'on possede (pour les
# mettre a jour/supprimer) sans jamais toucher a un media SERVICE externe
# (ex. "service-wfs" d'un flux IGN, cree par un autre processus).
NOEUD_SUFFIX = "-noeud"

# Au-dela de ce nombre de couches, un media par couche fait exploser la
# taille de la resource (ex. une resource a 84 couches -> 252 nouveaux
# medias SERVICE -> le manager renvoie "request entity too large" lors du
# PUT, confirme en pratique sur une serie temporelle mensuelle). Repli assume
# sur la forme generique (1 media par protocole, sans parametre "layer" --
# ambigu de toute facon pour autant de couches) plutot que de bloquer
# l'ecriture de la resource entiere -- voir KNOWN_ISSUES.md.
LAYER_MEDIA_THRESHOLD = int(os.environ.get("RUDI_GEO_LAYER_MEDIA_THRESHOLD", "20"))


def _media_name(protocol, layer, per_layer):
    base = f"service-{protocol}{NOEUD_SUFFIX}"
    return f"{base}__{layer}" if per_layer else base


def _service_url(protocol, layer):
    base_url = {"wms": WMS_URL, "wfs": WFS_URL}.get(protocol)
    if base_url is not None:
        # Toujours generique, jamais de parametre LAYERS/TYPENAMES : teste
        # en pratique (MapStore, un explorateur WFS independant), aucun
        # client ne l'utilise pour preselectionner une couche sur un
        # GetCapabilities -- la norme OGC ne le permet pas, le document
        # retourne liste TOUJOURS la totalite des couches du service, quel
        # que soit ce qu'on ajoute dans l'URL. Essaye puis abandonne -- voir
        # KNOWN_ISSUES.md.
        return base_url
    # OGC API Features, lui, est reellement adressable par couche
    # (contrairement a WMS/WFS) : chaque collection a son propre endpoint
    # REST fonctionnel. On pointe la collection precise si on sait laquelle,
    # sinon la liste generale (repli au-dela du seuil).
    return f"{OGCAPI_COLLECTIONS_URL}/{layer}" if layer else OGCAPI_COLLECTIONS_URL


def plan_for_resource(writer, entry, apply_changes):
    local_id = entry["resource_local_id"]
    layers = entry["layers"]
    # OGC API Features seul beneficie d'un media par couche (dans la limite
    # du seuil) : c'est le seul des 3 protocoles ou la couche precise change
    # reellement l'URL de facon fonctionnelle (chaque collection a son
    # propre endpoint). WMS/WFS restent toujours generiques, quel que soit
    # le nombre de couches -- un parametre ou un media distinct par couche
    # n'y change rien cote clients reels (voir _service_url ci-dessus).
    ogcapi_per_layer = 1 < len(layers) <= LAYER_MEDIA_THRESHOLD
    existants = writer.filter_metadata_list({"local_id": local_id})
    if not existants:
        return f"  [ignore] {entry['resource_title'][:60]!r} : introuvable sur le noeud (local_id={local_id})"

    resource = existants[0]
    medias = resource.setdefault("available_formats", [])
    by_name = {m.get("media_name"): m for m in medias if m.get("media_type") == "SERVICE"}
    nos_medias_existants = {n for n in by_name if any(n.startswith(f"service-{p}{NOEUD_SUFFIX}") for p in PROTOCOLS)}

    layer_info = (f" -- couche(s) : {', '.join(layers)}" if len(layers) <= 3 else f" -- {len(layers)} couches")

    desired = {}
    for protocol, conf in PROTOCOLS.items():
        if protocol == "ogcapi":
            per_layer = ogcapi_per_layer
            couches = layers if per_layer else ([layers[0]] if len(layers) == 1 else [None])
        else:
            per_layer = False
            couches = [None]
        for layer in couches:
            media_name = _media_name(protocol, layer, per_layer)
            caption = conf["caption"] + (f" -- couche {layer}" if per_layer else layer_info)
            desired[media_name] = {
                "interface_contract": conf["interface_contract"],
                "url": _service_url(protocol, layer),
                "connector_parameters": None,
                "caption": caption,
            }

    changed = False
    lines = []

    for media_name, conf in desired.items():
        existant = by_name.get(media_name)
        existant_connector = (existant or {}).get("connector", {})
        if (
            existant
            and existant_connector.get("url") == conf["url"]
            and existant_connector.get("interface_contract") == conf["interface_contract"]
            and existant_connector.get("connector_parameters") == conf["connector_parameters"]
            and existant.get("media_caption") == conf["caption"]
        ):
            continue  # deja a jour, rien a faire
        connector = {"url": conf["url"], "interface_contract": conf["interface_contract"]}
        if conf["connector_parameters"]:
            connector["connector_parameters"] = conf["connector_parameters"]
        if existant:
            existant["connector"] = connector
            existant["media_caption"] = conf["caption"]
            lines.append(f"    maj {media_name} -> {conf['url']}")
        else:
            import uuid

            medias.append(
                {
                    "media_type": "SERVICE",
                    "media_id": str(uuid.uuid4()),
                    "media_name": media_name,
                    "media_caption": conf["caption"],
                    "connector": connector,
                }
            )
            lines.append(f"    ajout {media_name} -> {conf['url']}")
        changed = True

    # Nettoyage : un media qu'on possedait avant (ex. la forme a 1 seule
    # couche "service-wms-noeud") mais qui ne correspond plus a la forme
    # actuelle (ex. la resource a maintenant plusieurs couches, donc
    # "service-wms-noeud__<layer>") devient orphelin -- on le supprime
    # plutot que de laisser un lien perime trainer indefiniment.
    for obsolete_name in nos_medias_existants - set(desired):
        medias.remove(by_name[obsolete_name])
        lines.append(f"    supprime {obsolete_name} (forme perimee)")
        changed = True

    if not changed:
        return f"  [a jour] {entry['resource_title'][:60]!r}"

    header = f"  [{'ecrit' if apply_changes else 'a ecrire'}] {entry['resource_title'][:60]!r}"
    if apply_changes:
        _put_metadata(writer, resource)
    return "\n".join([header] + lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Ecrit reellement sur le noeud (sans ce flag : dry-run)")
    parser.add_argument(
        "--local-id", action="append", default=None, metavar="LOCAL_ID",
        help="Ne traiter que cette ressource (repetable). Sans cette option : toutes les ressources du manifest.",
    )
    args = parser.parse_args()

    if not MANIFEST_PATH.exists():
        print(f"{MANIFEST_PATH} introuvable -- lancer d'abord un cycle du synchroniseur.", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    resources = manifest.get("resources_with_layers", [])
    if args.local_id:
        voulus = set(args.local_id)
        trouves = {r["resource_local_id"] for r in resources} & voulus
        resources = [r for r in resources if r["resource_local_id"] in voulus]
        for absent in voulus - trouves:
            print(f"  [absent du manifest] local_id={absent} -- pas (encore) de couche rudi-mapserver pour cette ressource, ignore.")
    print(f"{len(resources)} ressource(s) a traiter.")
    if not args.apply:
        print("Mode dry-run (rien n'est ecrit) -- relancer avec --apply pour appliquer.\n")

    writer = RudiNodeWriter(pm_url=MANAGER_URL, auth=RudiNodeAuth(usr=RUDI_USER, pwd=RUDI_PASSWORD))

    echecs = []
    for entry in resources:
        try:
            print(plan_for_resource(writer, entry, args.apply))
        except Exception as e:
            # Deja observe : le manager renvoie parfois une erreur HTTP
            # ("request entity too large") alors que l'ecriture a reellement
            # reussi cote catalogue (verifie en relisant la resource juste
            # apres) -- cause exacte non identifiee, probablement cote
            # construction de la reponse plutot que de l'ecriture elle-meme.
            # On ne bloque jamais tout le lot pour une ressource : la
            # prochaine execution revérifiera son etat (idempotent) et ne
            # la retraitera que si necessaire.
            echecs.append(entry["resource_local_id"])
            print(f"  [ERREUR] {entry['resource_title'][:60]!r} : {e}")

    if echecs:
        print(f"\n{len(echecs)} ressource(s) en erreur (voir ci-dessus) -- relancer le script verifiera leur etat reel.")


if __name__ == "__main__":
    main()
