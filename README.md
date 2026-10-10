# rudi-mapserver

Brique optionnelle et complementaire pour un noeud Rudi producteur : elle
expose les fichiers deja publies sur le noeud comme des flux cartographiques
standards -- **WMS**, **WFS** et **OGC API Features** -- sans rien copier, sans
base de donnees, et sans modifier le noeud lui-meme.

**Depot compagnon autonome**, deploye a cote d'un noeud Rudi deja en marche,
jamais fusionne dedans : il n'utilise que des images toutes faites (MapServer,
nginx), n'a pas besoin d'etre construit ni couple au pipeline de build du
noeud. On l'ajoute, ou on le retire, sans rien changer d'autre.

## Comment ca marche

Un noeud Rudi republie deja des fichiers (CSV, GeoJSON, GeoPackage...) comme
medias `FILE` telechargeables. rudi-mapserver prend ces memes fichiers, en
lecture seule, et les sert en plus comme des couches cartographiques
interrogeables -- utile pour afficher une carte (MapStore, QGIS...) ou
consommer les donnees via une API REST moderne (OGC API Features), sans
telecharger le fichier entier.

Deux pieces travaillent ensemble :

- **`geo-sync`** interroge periodiquement le catalogue du noeud, repere les
  fichiers geographiques exploitables parmi les medias publies, et regenere a
  chaque cycle un mapfile MapServer a jour. Il peut aussi, en option,
  declarer lui-meme ces flux sur le catalogue (voir plus bas) pour qu'ils
  soient visibles a cote des autres medias d'une ressource.
- **`mapserver`** sert ce mapfile : une seule brique pour les trois
  protocoles (WMS, WFS, OGC API Features), lue directement depuis le volume
  de fichiers du noeud -- aucune copie, aucune synchronisation de donnees,
  juste un index regenere.

Deux services additionnels, optionnels mais pratiques : `mapserver-tls`
(terminaison HTTPS locale, pour des clients qui l'exigent) et `geo-metadata`
(fiches de metadonnees XML par couche).

## Deploiement

### Prerequis

- Docker avec le plugin Compose (commande `docker compose`).
- Un noeud Rudi deja demarre : catalogue et manager joignables, fichiers
  presents dans son repertoire de medias.

### Etapes

1. **Cloner le depot** :
   ```bash
   git clone <url> rudi-mapserver
   cd rudi-mapserver
   ```

2. **Configurer** : copier le modele et l'ajuster.
   ```bash
   cp .env.example .env
   ```
   Au minimum, verifiez et corrigez si besoin (voir `.env.example` pour le
   detail de chaque variable) :
   - `RUDI_NODE_MEDIA_DIR` : le chemin vers le repertoire de medias de votre
     noeud (par defaut, suppose un `rudi-node-container` clone juste a cote).
   - `RUDI_CATALOG_PORT` / `RUDI_MANAGER_PORT` : les ports reels de votre
     noeud. Les valeurs par defaut (3030/3032) correspondent a un
     deploiement standard de `rudi-node-container`. Si votre installation
     utilise d'autres ports (par exemple un environnement de developpement
     personnalise), ajustez-les -- une mauvaise valeur se traduit par un
     synchroniseur qui ne trouve pas le catalogue, ou une declaration de
     flux qui echoue en silence (voir "Declarer les flux" plus bas).
   - `RUDI_USER` / `RUDI_PASSWORD` : identifiants d'un compte habilite sur le
     noeud (necessaires uniquement si vous utilisez la declaration des flux
     sur le catalogue, voir plus bas).

3. **Demarrer la stack** :
   ```bash
   docker compose -f docker-compose.geo.yml up -d --build
   ```

4. **Verifier** : le premier cycle du synchroniseur prend quelques dizaines
   de secondes (lecture du catalogue, generation du mapfile).
   ```bash
   sleep 30
   python3 test_services.py http://localhost:8089
   ```
   8 controles doivent passer (WMS, WFS, OGC API Features). En cas d'echec,
   `docker compose -f docker-compose.geo.yml logs geo-sync` est le premier
   reflexe -- il journalise chaque cycle et chaque erreur de connexion au
   noeud.

C'est tout : la stack tourne, les couches disponibles dependent de ce que le
noeud publie deja (voir "Endpoints" pour y acceder, et "Declarer les flux"
pour les rendre visibles directement dans le catalogue Rudi).

### Si le noeud tourne aussi en conteneurs sur la meme machine

Par defaut, `geo-sync` joint le catalogue et le manager du noeud via
`host.docker.internal` et leurs ports publies sur l'hote. Si vous preferez
ne pas dependre de ports hote publies (par exemple un manager que vous avez
choisi de ne plus exposer publiquement), un override optionnel fait
rejoindre a `geo-sync` le reseau Docker du noeud directement, et joindre ses
services par leur nom :

```bash
echo "RUDI_MANAGER_HOST=rudi-manager" >> .env
echo "RUDI_CATALOG_HOST=rudi-catalog" >> .env
docker compose -f docker-compose.geo.yml -f docker-compose.geo.network.yml up -d --build
```

`docker-compose.geo.network.yml` suppose que le reseau Docker du noeud
s'appelle `rudinode_rudi-net` (nom par defaut pour un deploiement a services
separes) ; surchargez-le via `RUDI_NODE_NETWORK` dans `.env` si besoin. Non
applicable si le noeud tourne en `network_mode: host` (dans ce cas,
`host.docker.internal` joint deja directement l'hote, aucun changement
necessaire). Dans tous les cas, cette etape reste **optionnelle** : le
comportement par defaut fonctionne sans elle.

## Endpoints

- **WMS** : `http://localhost:8089/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities`
- **WFS** : `http://localhost:8089/?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetCapabilities`
- **OGC API Features** : `http://localhost:8089/RUDI/ogcapi/collections`
  (l'alias `RUDI` doit etre le **premier segment du chemin** -- convention
  REST de MapServer, RFC ms-rfc-134 -- jamais un `?map=` classique comme pour
  WMS/WFS, voir `KNOWN_ISSUES.md` pour le detail de l'erreur sinon obtenue)
- **Metadonnees par couche** : une fiche XML (profil ISO 19139/TC211) par
  couche, servie sur le port 8090, referencee dans `<MetadataURL>` des
  Capabilities WMS/WFS.
- **Recensement des flux externes deja references dans le catalogue** (ex.
  services tiers comme data.geopf.fr) : `http://localhost:8090/manifest.json`,
  cle `external_services`. Ces flux sont seulement listes, jamais relayes --
  les relayer engagerait rudi-mapserver sur leur disponibilite et souleverait
  une question d'attribution hors de son perimetre.

Le nom de chaque couche derive du nom du fichier sur le volume de medias
(normalise, prefixe de `l_` s'il commencerait par un chiffre -- contrainte
WMS/OGC).

### Acces HTTPS (clients publics, navigateurs)

Le service `mapserver-tls` sert le meme contenu en HTTPS sur le port 8444
(certificat de developpement local genere par `mkcert`, jamais commite) --
necessaire pour qu'une page cliente chargee en HTTPS puisse afficher des
tuiles WMS sans blocage navigateur ("mixed content"). Avant le premier usage,
accepter l'exception de certificat en visitant une fois
`https://localhost:8444/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities`
dans le navigateur (ou faire confiance au CA mkcert avec `mkcert -install`).

**En production**, deux options :
- deposer un vrai certificat (memes noms de fichiers que ceux attendus par
  `mapfiles/nginx-tls.conf`) dans `mapfiles/tls/` ;
- ou supprimer le service `mapserver-tls` et faire terminer le TLS par le
  reverse proxy deja en place devant votre noeud, en ajustant
  `NODE_PUBLIC_URL` / `OGCAPI_PUBLIC_URL` / `RUDI_GEO_PUBLIC_BASE` vers vos
  URLs HTTPS publiques reelles.

`NODE_PUBLIC_URL` doit toujours etre une URL reellement fonctionnelle :
MapServer l'utilise pour construire ses propres liens de retour (pagination
WFS, metadonnees) -- une adresse non testee y casse silencieusement ces
liens, sans erreur visible cote client.

Important : apres toute recreation du conteneur `mapserver` (mise a jour du
mapfile, redemarrage...), recreer aussi `mapserver-tls` -- sinon son proxy
garde l'ancienne adresse interne et renvoie un `403 Forbidden` trompeur :
```bash
docker compose -f docker-compose.geo.yml up -d --force-recreate mapserver-tls
```

## Declarer les flux dans le catalogue Rudi

Au-dela de servir les flux, `declare_services.py` les **declare sur le
catalogue** : il ajoute, sur chaque ressource dont au moins une couche est
publiee, un media `SERVICE` par protocole (WMS, WFS, OGC API Features)
pointant vers ces flux -- pour que le catalogue et le portail Rudi les
affichent, exactement comme ils le font deja pour des flux externes (ex.
`data.geopf.fr`).

- `service-wms-noeud`, `service-wfs-noeud` : un seul media generique par
  ressource, quel que soit le nombre de couches (la norme WMS/WFS ne permet
  pas de cibler une couche precise via l'URL de service).
- `service-ogcapi-noeud` : un media par couche si la ressource en a
  plusieurs (jusqu'a `LAYER_MEDIA_THRESHOLD` couches, 20 par defaut --
  au-dela, repli sur la liste generale des collections) -- seul protocole ou
  chaque couche a reellement son propre endpoint.

Deux facons de declarer, **independantes et sans conflit** (le script est
idempotent : il ne reecrit que ce qui a change) :

1. **Automatique**, depuis `geo-sync` lui-meme : apres chaque cycle ou le
   jeu de couches publiees change, le conteneur appelle
   `declare_services.py --apply` (voir `synchronizer/sync.py::self_declare`,
   desactivable via `RUDI_GEO_SELF_DECLARE=false`). C'est ce qui permet a un
   simple `docker compose up` de tout declarer, sans rien d'autre a brancher.
2. **Manuelle**, depuis n'importe quelle machine ayant acces au manager du
   noeud :
   ```bash
   pip install --user rudi-node-write
   python3 declare_services.py            # dry-run : affiche ce qui serait fait
   python3 declare_services.py --apply    # ecrit reellement sur le noeud
   ```
   `--local-id <uuid>` (repetable) restreint le traitement a une ou
   plusieurs ressources precises -- utile pour n'importe quel pipeline
   externe qui voudrait ne declarer que les ressources qu'il vient de
   publier, sans retraiter tout le catalogue a chaque fois.

## Fichiers

- `synchronizer/sync.py` -- lit le catalogue, inspecte les fichiers via OGR,
  regenere `generated/rudi.map` en entier a chaque cycle (defaut : 60s,
  ecriture atomique), puis appelle `declare_services.py` si le jeu de couches
  a change.
- `Dockerfile.geo-sync` -- image du conteneur `geo-sync` : l'image MapServer
  de base plus `python3-pip` et `rudi-node-write`, pour qu'il puisse
  declarer les services lui-meme.
- `.env.example` -- modele de configuration (copier en `.env`, jamais
  commite).
- `docker-compose.geo.network.yml` -- override optionnel : fait rejoindre a
  `geo-sync` le reseau Docker du noeud (voir "Deploiement" plus haut).
- `mapfiles/mapserver.conf` -- config MapServer (alias `RUDI`, lecture
  seule).
- `mapfiles/apache-pna.conf`, `mapfiles/nginx-tls.conf`, `mapfiles/tls/` --
  Private Network Access et terminaison HTTPS locale.
- `mapfiles/nginx-metadata.conf` -- service statique dedie (port 8090) pour
  les fiches XML de metadonnees et `manifest.json`.
- `generated/` -- sortie du synchroniseur (mapfile, VRT, manifest.json,
  FlatGeobuf) ; jamais commite, regenere a chaque demarrage.
- `test_services.py` -- recette automatique (8 controles).
- `declare_services.py` -- declare les flux comme medias `SERVICE` sur les
  ressources du catalogue (voir ci-dessus).
- `KNOWN_ISSUES.md` -- limites et comportements constates en pratique,
  utile en complement de ce README pour le detail technique et le
  depannage.

## Fichiers volumineux : index spatial FlatGeobuf

Au-dela de `FLATGEOBUF_THRESHOLD_MB` (defaut 5 Mo), le synchroniseur
convertit la source en [FlatGeobuf](https://flatgeobuf.org/) (format GDAL
avec index spatial integre) dans `generated/fgb/` : MapServer peut alors
filtrer par bbox sans relire tout le fichier a chaque requete WMS/WFS.
Regenere uniquement si absent ou plus vieux que la source. C'est une
**derogation assumee** au "zero copie", limitee aux quelques fichiers qui en
ont reellement besoin -- traite comme un cache jetable, jamais une source de
verite.

## Limites connues

Voir `KNOWN_ISSUES.md` pour le detail technique complet : comportements
particuliers de MapServer et de certains clients (MapStore...), choix de
conception assumes, et un chantier identifie mais hors scope -- l'acces
restreint aux donnees chiffrees du noeud, a ouvrir avec l'equipe Rudi le cas
echeant.
