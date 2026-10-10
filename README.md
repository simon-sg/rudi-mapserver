# rudi-mapserver -- services SIG pour le noeud Rudi source local

Implementation pilote de la note d'architecture "Services SIG sur les fichiers
d'un noeud Rudi" : MapServer 8 a cote du noeud, lecture seule, pas de copie de
donnees, synchroniseur qui regenere a chaque cycle un mapfile MapServer servant
WMS, WFS *et* OGC API Features -- les trois protocoles, une seule brique,
exactement la recommandation de la note.

**Depot compagnon autonome de `rudi-node-container`, pas un module de ce
depot** : il ne s'appuie que sur des images toutes faites (MapServer, nginx) et
se deploie a cote d'un noeud Rudi deja en marche, sans toucher a son pipeline de
build. Voir `KNOWN_ISSUES.md` pour les limites constatees pendant le pilotage.

## Deployer

Prerequis : Docker avec le plugin Compose (commande `docker compose`), et un
`rudi-node-container` deja demarre (catalog et manager joignables, fichiers dans
`source-media/`).

**Verifiez les ports reels de votre noeud avant de lancer la stack.**
`.env.example` est livre avec les ports du harnais de developpement local
(`RUDI_CATALOG_PORT=4030`, `RUDI_MANAGER_PORT=4032`) ; un `rudi-node-container`
standard publie en realite le catalogue sur le port 3030 et le manager sur le
port 3032 (dans ses quatre variantes de deploiement). Si vous visez un
`rudi-node-container`, ajustez `RUDI_CATALOG_PORT` et `RUDI_MANAGER_PORT` dans
`.env` en consequence avant de lancer `docker compose up` (sinon le
synchroniseur ne trouve pas le catalogue, et le self-declare du manager echoue
en warning silencieux).

```bash
git clone <url> rudi-mapserver
cd rudi-mapserver
cp .env.example .env            # ajuster si le voisinage differe du defaut
docker compose -f docker-compose.geo.yml up -d --build
sleep 30                        # premier cycle du synchroniseur (lecture du catalogue + generation)
python3 test_services.py http://localhost:8089   # recette automatique : 8/8 attendu
```

Le fichier `.env` (modele dans `.env.example`) permet d'ajuster le chemin du
volume media du noeud (`RUDI_NODE_MEDIA_DIR`), l'hote et les ports du catalogue
et du manager, les identifiants Rudi, et l'URL publique des flux. Sans `.env`,
les defauts supposent un `rudi-node-container` voisin
(`../rudi-node-container/source-media`) ; pensez a verifier les ports comme
indique ci-dessus avant de lancer la stack.

Le noeud source natif tourne via `rudi-node-build/start-source-node.sh` (catalog
sur le port 4030, manager sur le port 4032, tous deux publies sur `127.0.0.1`
uniquement) et `rudi-node-build/rudi-node-container/source-media/` contient
les fichiers ; ce depot ne fait que les lire en lecture seule. C'est ce harnais
local qui a dicte les defauts `4030`/`4032` de `.env.example`.

## Joindre le noeud via son reseau Docker (au lieu des ports hote)

Par defaut, `geo-sync` joint le catalogue et le manager via
`host.docker.internal` et des ports hote publies. Si le noeud tourne sur la
meme machine en conteneurs et que l'on veut eviter de dependre de ports hote
publies (par exemple un manager durci plus tard, ou un pare-feu local), un
override optionnel fait rejoindre a `geo-sync` le reseau Docker externe du
noeud, pour joindre `rudi-manager`/`rudi-catalog` par leurs noms de service :

```bash
# Si rudi-node-container tourne sur la meme machine en conteneurs et
# qu'on veut eviter de dependre de ports hote publies :
echo "RUDI_MANAGER_HOST=rudi-manager" >> .env
echo "RUDI_CATALOG_HOST=rudi-catalog" >> .env
docker compose -f docker-compose.geo.yml -f docker-compose.geo.network.yml up -d --build
```

`docker-compose.geo.network.yml` suppose que le reseau Docker du noeud s'appelle
`rudinode_rudi-net` (defaut de `rudi-node-container` pour ses variantes a
services separes) ; surchargez-le via `RUDI_NODE_NETWORK` dans `.env` s'il
differe. Cette variante ne s'applique pas a `-host.yml` (deja en
`network_mode: host`, ou `host.docker.internal` revient a joindre directement
l'hote). Reste **optionnel** : le comportement par defaut
(`host.docker.internal`) fonctionne sans cette etape.

## Endpoints

- WMS : `http://localhost:8089/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities`
- WFS : `http://localhost:8089/?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetCapabilities`
  (`?map=RUDI&` reste accepte mais n'est plus necessaire : un seul mapfile
  existe, il est utilise par defaut -- voir `KNOWN_ISSUES.md` point 9, evite
  en particulier le piege d'un catalogue MapStore qui concatene betement ses
  propres parametres a la suite d'une URL deja longue)
- OGC API Features : `http://localhost:8089/RUDI/ogcapi/collections`
  (l'alias `RUDI` doit etre le **premier segment du chemin**, jamais un `?map=`
  classique -- c'est la convention REST de MapServer, RFC ms-rfc-134 ; voir
  `KNOWN_ISSUES.md` point 1 pour l'erreur obtenue si on s'y prend comme pour le
  WMS/WFS)
- Metadonnees de couche : une fiche XML (profil ISO 19139/TC211) par couche,
  servie par un service statique dedie sur le port 8090 (jamais par
  `mapserver` lui-meme, voir `KNOWN_ISSUES.md` point 5), referencee dans
  `<MetadataURL>` de chaque couche des Capabilities WMS/WFS.
- Recensement des flux WFS/WMS externes deja references dans le catalogue
  Rudi (medias `SERVICE`, ex. data.geopf.fr) : `http://localhost:8090/
  manifest.json`, cle `external_services`. Ces flux ne sont **ni interroges,
  ni relayes** par le noeud -- seulement listes avec leur URL d'origine, pour
  qu'on sache qu'ils existent a cote des couches republiees depuis les
  fichiers. Choix assume : relayer un service tiers engagerait le noeud sur
  sa disponibilite et poserait une question d'attribution hors scope du
  pilote.

Le nom de chaque couche = nom du fichier sur le volume media (slugifie, prefixe
de `l_` s'il commencerait par un chiffre -- voir `KNOWN_ISSUES.md` point 2), par
exemple `l_6e727f36_afb3_4c81_9fc4_5a331b661df0_canton` pour
`6e727f36-...-canton.geojson`.

### Acceder au nœud depuis un client HTTPS public (ex. MapStore public)

En plus du `mapserver` en HTTP (port 8089), un service `mapserver-tls` sert le
meme contenu en HTTPS sur le port 8444, certificat local genere par `mkcert`
(`mapfiles/tls/`, jamais commite). Necessaire pour qu'une page cliente en HTTPS
charge les tuiles WMS sans blocage navigateur ("mixed content") -- voir
`KNOWN_ISSUES.md` point 4. Avant le premier usage, visiter une fois
`https://localhost:8444/?map=RUDI&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities`
dans le navigateur pour accepter l'exception de certificat (ou faire confiance
au CA mkcert avec `mkcert -install`, demande sudo).

Port 8444, PAS 8443 : `apigateway` (portail Rudi) ecoute lui-meme sur 8443 en
interne. Si notre `connector.url` de service (voir plus bas) pointe sur
`localhost:8443`, le relais du portail (`/medias/.../wms`) boucle
silencieusement sur apigateway lui-meme au lieu de nous joindre -- voir
`KNOWN_ISSUES.md` pour le detail (point sur la fausse redirection `/login`).

**Note production** : le certificat `mkcert` de `mapfiles/tls/` est un confort
de developpement local. En production, deux options :

- deposer un vrai certificat (server + cle, memes noms de fichiers que ceux
  attendus par `mapfiles/nginx-tls.conf`) dans `mapfiles/tls/` ;
- ou supprimer le service `mapserver-tls` et faire terminer le TLS par le
  reverse proxy deja en place devant le noeud reel en production, en ajustant
  `NODE_PUBLIC_URL` / `OGCAPI_PUBLIC_URL` (et les URLs `RUDI_GEO_*` utilisees
  par `declare_services.py`) vers les URLs HTTPS publiques du noeud.

Important : apres toute recreation du conteneur `mapserver` (mise a jour du
mapfile, redemarrage...), recreer aussi `mapserver-tls` -- sinon son proxy
garde l'ancienne adresse interne et renvoie un `403 Forbidden` trompeur sur
tout (voir `KNOWN_ISSUES.md` point 7) :
```bash
docker compose -f docker-compose.geo.yml up -d --force-recreate mapserver-tls
```

`NODE_PUBLIC_URL` (defaut `https://localhost:8444`) doit etre une URL
reellement fonctionnelle : MapServer l'utilise pour construire ses propres
liens de retour (pagination WFS, metadonnees WFS) -- une adresse de
confort non testee y casse silencieusement ces liens, sans erreur visible
cote client (voir `KNOWN_ISSUES.md` point 8).

## Declarer les flux WMS/WFS/OGC API dans le catalogue Rudi

`declare_services.py` ajoute, sur chaque resource dont au moins une couche
est publiee, un media `SERVICE` par protocole (WMS, WFS, OGC API Features)
pointant vers nos propres flux -- pour que le catalogue/portail Rudi les
affiche, comme il le fait deja pour des flux externes (ex. `data.geopf.fr`,
voir le recensement `external_services` ci-dessus). Noms de media distincts
de ces derniers : jamais de confusion ni d'ecrasement entre un flux tiers et
le notre sur la meme ressource.

- `service-wms-noeud`, `service-wfs-noeud` : toujours **un seul media
  generique par resource**, quel que soit le nombre de couches. Essaye et
  abandonne : un media (ou un parametre d'URL) par couche pour WMS/WFS
  (`&LAYERS=`/`&TYPENAMES=`, ou `connector_parameters.layer`) -- aucun
  client reel (MapStore, un explorateur WFS independant) n'en tient compte
  pour preselectionner une couche, `GetCapabilities` renvoie toujours la
  liste complete du service quel que soit ce qu'on y ajoute (limite du
  protocole, pas de cette implementation -- voir `KNOWN_ISSUES.md`).
- `service-ogcapi-noeud` : seul protocole ou une couche precise change
  reellement l'URL (chaque collection a son propre endpoint REST). Un media
  par couche si la resource en a plusieurs (`service-ogcapi-noeud__<layer>`),
  dans la limite de `LAYER_MEDIA_THRESHOLD` couches (20 par defaut) --
  au-dela, repli sur la liste generale des collections (voir
  `KNOWN_ISSUES.md`, evite un `PUT` en echec sur une resource a tres
  grand nombre de couches). Si la forme change (ex. une couche de plus
  apparait), les medias de l'ancienne forme sont supprimes plutot que
  laisses perimes.

Deux appels coexistent, **independants et sans conflit** (le script est
idempotent : il ne reecrit que ce qui a reellement change) :

1. **Automatique, depuis `geo-sync`** : apres chaque cycle du synchroniseur ou
   le jeu de couches publiees a change, le conteneur `geo-sync` appelle lui-meme
   `declare_services.py --apply` (voir `synchronizer/sync.py::self_declare`,
   desactivable via `RUDI_GEO_SELF_DECLARE=false`). C'est ce qui permet a un
   simple `docker compose up` de tout declarer, sans pipeline externe.
2. **Depuis l'hote**, pour un humain ou le pipeline `moissonneuse-batteuse`
   (voisin, voir ci-dessous), avec la librairie installee localement via l'API
   privee du noeud (module manager, port 4032) :
   ```bash
   pip install --user rudi-node-write
   python3 declare_services.py            # dry-run : affiche ce qui serait fait
   python3 declare_services.py --apply    # ecrit reellement sur le noeud
   ```
   Idempotent (relancer sans rien avoir change ne reecrit rien) ; lit
   `generated/manifest.json` (cle `resources_with_layers`, ecrite par
   `sync.py`), ne reinterroge jamais le catalogue pour decouvrir les couches.

`--local-id <uuid>` (repetable) restreint le traitement a une ou plusieurs
ressources precises -- utilise par le pipeline `moissonneuse-batteuse`
(voisin, voir ci-dessous) pour ne declarer que les JDD effectivement
(re)publies lors d'un run donne, sans retraiter tout le catalogue a chaque
fois :
```bash
python3 declare_services.py --apply --local-id <uuid1> --local-id <uuid2>
```

### Appele automatiquement par le pipeline `moissonneuse-batteuse`

Le projet voisin `moissonneuse-batteuse` (moissonneur de donnees ouvertes
vers ce meme noeud) appelle ce script en derniere etape de son pipeline
(`src/publish_rudi_geo.py`), uniquement pour les JDD effectivement
(re)publies pendant ce run (son propre mecanisme de detection de
changement, en amont de la publication RUDI, fait deja office de filtre
"nouveau ou modifie" -- rien a dupliquer ici). Si les conteneurs rudi-mapserver
ne tournent pas, `moissonneuse-batteuse` tente de les demarrer (meme
pattern que pour le noeud RUDI lui-meme) ; best-effort, une indisponibilite
de rudi-mapserver ne bloque jamais la publication des donnees elle-meme.

## Fichiers

- `synchronizer/sync.py` -- lit le catalogue, inspecte les fichiers via OGR,
  regenere `generated/rudi.map` en entier a chaque cycle (defaut : 60s,
  ecriture atomique), puis appelle `declare_services.py` si le jeu de couches a
  change. Pour chaque couche, enveloppe la source dans un second VRT OGR qui
  materialise un identifiant reel (`rudi_fid`), necessaire pour OGC API
  Features (voir `KNOWN_ISSUES.md` point 1).
- `Dockerfile.geo-sync` -- image du conteneur `geo-sync` : l'image MapServer
  (`camptocamp/mapserver:8.6.0`, sans pip) plus `python3-pip` et
  `rudi-node-write`, afin que `geo-sync` puisse declarer les services lui-meme.
- `.env.example` -- modele de configuration (a copier en `.env`, jamais commite).
- `docker-compose.geo.network.yml` -- override optionnel : fait rejoindre a
  `geo-sync` le reseau Docker externe du noeud (voir ci-dessus).
- `mapfiles/mapserver.conf` -- config MapServer (alias `RUDI`, lecture seule).
- `mapfiles/apache-pna.conf`, `mapfiles/nginx-tls.conf`, `mapfiles/tls/` --
  Private Network Access + terminaison HTTPS locale pour les clients publics
  (voir ci-dessus et `KNOWN_ISSUES.md` point 4).
- `mapfiles/nginx-metadata.conf`, `generated/metadata/` -- service statique
  dedie (port 8090) pour les fiches XML de metadonnees par couche (voir
  `KNOWN_ISSUES.md` point 5) et pour `manifest.json` (dont le recensement
  des flux WFS/WMS externes, cle `external_services`, voir ci-dessus).
- `generated/` -- sortie du synchroniseur (mapfile, VRT des CSV et des FID,
  manifest.json, FlatGeobuf ; jamais commite -- regenere a chaque demarrage).
- `test_services.py` -- recette automatique (8/8 controles, voir
  `KNOWN_ISSUES.md` pour le detail des bugs rencontres et corriges en route).
- `declare_services.py` -- declare nos flux WMS/WFS/OGC API comme medias
  SERVICE sur les resources du catalogue (voir ci-dessus).
- `KNOWN_ISSUES.md` -- limites et bugs constates pendant le montage du pilote,
  a reporter dans la note d'architecture.

## Fichiers volumineux : index spatial FlatGeobuf

Au-dela de `FLATGEOBUF_THRESHOLD_MB` (defaut 5 Mo -- 13 fichiers sur 555
couches actuellement), le synchroniseur convertit la source en
[FlatGeobuf](https://flatgeobuf.org/) (format GDAL avec index spatial
integre) via `ogr2ogr`, dans `generated/fgb/` : MapServer peut alors
filtrer par bbox sans relire tout le fichier a chaque requete WMS/WFS.
Regenere uniquement si absent ou plus vieux que la source (jamais a chaque
cycle). C'est une **deroge assumee** au "zero copie" du pilote, limitee aux
quelques fichiers qui en ont reellement besoin (voir `KNOWN_ISSUES.md`
point 10ter) -- traite comme un cache jetable, jamais une source de verite.
Marque `"flatgeobuf": true` sur la couche dans `manifest.json`.

A distinguer du plafond `wfs_maxfeatures` (point 10), qui limite la
*taille de la reponse* WFS mais ne traitait pas, a lui seul, la lenteur du
*dessin* WMS sur un gros fichier.

## Ce qui n'est pas fait (voir la note, section "Plan de mise en oeuvre")

- Chantier "acces restreint" (hors scope, a ouvrir avec l'equipe Rudi).
