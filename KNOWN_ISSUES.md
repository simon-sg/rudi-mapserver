# Limites connues du pilote (constatees le 2026-10-09)

Issues trouvees en montant le pilote decrit dans la note d'architecture
"Services SIG sur les fichiers d'un noeud Rudi", sur `camptocamp/mapserver:8.6.0`
(MapServer 8.6.0 / GDAL 3.12.1), contre les vraies donnees du noeud source local
(`rudi-node-build/rudi-node-container/source-media`).

## 1. OGC API Features -- RESOLU : c'est un probleme d'URL, pas de bug bloquant

Premiere piste (abandonnee) : un classique `?map=RUDI` combine a un chemin REST
(`/collections`) echoue toujours, quel que soit le serveur HTTP devant `mapserv` --
reproduit a l'identique derriere Apache+mod_fcgid (image par defaut) et derriere
nginx avec un `fastcgi_pass` direct sur `mapserv` lance en FastCGI brut
(`SERVER=spawn-fcgi`), et sur deux versions de MapServer (8.4.1 stable et le
8.7-dev embarque par le tag `camptocamp/mapserver:8.6.0`, cf. `mapserv -v`) :
`msCGILoadMap(): CGI variable "map" not found in configuration and this server is
not configured for full paths.` Ce test croise (webserver x version) confirme que
ce n'est ni un souci de proxy/Apache ni une regression du build "dev" de l'image --
**la cause reelle est que l'URL etait fausse.**

**La RFC MapServer ms-rfc-134 (OGC API) est explicite** : en mode REST, l'alias du
mapfile doit etre le **premier segment du chemin**, jamais passe par `?map=` --
`/<mapfile>/ogcapi/<route>`, par ex. `/RUDI/ogcapi/collections`. Une fois l'URL
corrigee, OGC API Features fonctionne **nativement dans MapServer, sans aucune
brique supplementaire** : `/RUDI/ogcapi/collections` liste les 581 collections
publiees, `/RUDI/ogcapi/collections/<id>/items` retourne les features --
confirme avec succes en combinant les deux memes elements qui echouaient avant
(nginx+fastcgi_pass brut, et MapServer 8.4.1 stable).

Deuxieme ecueil, plus discret, rencontre en testant `/items` : MapServer exige que
`gml_featureid` pointe vers un champ qui existe reellement dans les donnees -- avec
la valeur litterale `"FID"` (qui n'est un vrai champ sur aucun de nos fichiers), on
obtient `Error getting feature. Feature id not found.` La parade : chaque couche
est enveloppee dans un second VRT OGR (`write_fid_vrt` dans `sync.py`) qui
materialise l'identifiant interne OGR en un champ reel via une requete OGR SQL
passthrough (`SELECT *, FID AS rudi_fid FROM "<couche>"`), et `gml_featureid`
pointe vers ce `rudi_fid`. Fonctionne identiquement pour les GeoJSON/GPKG directs
et pour les CSV (VRT a deux etages : le VRT "points depuis colonnes X/Y" existant
devient la source du VRT "FID").

Conclusion : le flux "pygeoapi + MapServer" compare dans la note n'est pas
necessaire ici -- l'architecture a une seule brique ("MapServer seul") qu'elle
recommandait fonctionne telle que prevue pour les trois clients, WMS/WFS *et*
OGC API Features compris.

## 2. MapStore refuse silencieusement les couches dont le nom commence par un chiffre -- RESOLU

Teste en conditions reelles le 2026-10-09 contre le MapStore public de Rennes
Metropole (`https://public.sig.rennesmetropole.fr/mapstore/#/`), en ajoutant le
WMS du noeud comme nouveau service catalogue (voir aussi le point 3 ci-dessous
pour le prealable HTTPS). Le catalogue affichait bien les couches (GetCapabilities
OK), mais toute couche ajoutee a la carte affichait une icone d'erreur generique
(« La couche n'a pas ete chargee correctement ou n'est pas disponible ») sans
detail exploitable, alors que les memes requetes WMS GetMap/GetCapabilities
testees directement en curl (CRS, BBOX, tuilage 512x512, `TILED=true` -- tout ce
que MapStore envoie reellement) repondaient 200 avec une image valide.

La cause etait dans le XML des Capacites lui-meme : MapServer y insere un
commentaire `<!-- WARNING: The layer name '...' might contain spaces or invalid
characters or may start with a number. -->` pour 222 couches sur 581 -- nos
noms de couche reprennent le `media_id` (un UUID), et la moitie des UUID
commencent par un chiffre. Un nom de couche WMS/XML doit etre un NCName valide
(jamais de chiffre en tete) ; MapServer tolere et sert quand meme la couche,
mais MapStore la rejette silencieusement cote client.

**Correction** : `slugify()` dans `sync.py` prefixe desormais tout nom qui
commencerait par un chiffre avec `l_` (ex. `6e727f36_..._canton` devient
`l_6e727f36_..._canton`). Plus aucun warning dans les Capacites apres
regeneration, et la couche s'affiche correctement dans le MapStore public une
fois re-ajoutee depuis le catalogue. A reporter imperativement dans toute
convention de nommage de couche derivee d'un UUID.

## 3. Toute couche ponctuelle (CSV) etait invisible -- RESOLU

Decouvert en voulant reproduire le test MapStore public (point 2) sur une
*autre* famille de couches (IRVE, ponctuelle) apres correction du point 2 :
meme symptome ("La couche n'a pas ete chargee correctement"), mais cause
totalement differente -- le nom de la couche IRVE est valide (ne commence pas
par un chiffre), seul le rendu plantait.

En rejouant la requete GetMap exacte en curl (cette fois sur une tuile qui
contient reellement des points, pas une tuile vide -- piege initial : un
premier test sur une tuile sans donnee "reussissait" en silence, 200 OK mais
image entierement transparente) :

```
<ServiceException>
msDrawMap(): Image handling error. Failed to draw layer named 'aace95ff_...'.
msLoadMSRasterBufferFromFile(): General error message. unable to open file
/etc/mapserver/generated/circle for reading
</ServiceException>
```

Cause : le style des couches ponctuelles reference `SYMBOL "circle"` par son
nom, mais aucun `SYMBOLSET` ne definit jamais ce symbole dans le mapfile --
MapServer tente alors de l'interpreter comme un chemin de fichier et echoue
des qu'au moins un point tombe dans la tuile demandee (CRS indifferent, testee
en EPSG:4326 et EPSG:3857). Touchait **toutes** les couches ponctuelles (CSV),
pas seulement IRVE -- les couches polygonales (ex. `canton`, deja validee dans
le vrai MapStore, point 2) n'utilisent pas de `SYMBOL` et n'etaient pas
affectees, ce qui a complique le diagnostic en faisant croire a un probleme
specifique a IRVE.

**Correction** : un bloc `SYMBOL NAME "circle" TYPE ELLIPSE FILLED TRUE POINTS
1 1 END END` ajoute une fois dans l'en-tete du mapfile (`MAP_HEADER` dans
`sync.py`), avant les `LAYER`. Confirme par capture de pixels (couleur du
style `200 50 50` bien presente dans le PNG) sur une tuile connue pour
contenir des points IRVE.

## 4. Depuis une page HTTPS publique, acceder a un WMS local en HTTP

Pour ce meme test MapStore public (page HTTPS) -> noeud local (HTTP), deux
obstacles independants a lever, dans cet ordre :

- **Private Network Access (Chrome)** : une page publique qui appelle une
  adresse privee/loopback doit recevoir `Access-Control-Allow-Private-Network:
  true` sur le preflight OPTIONS, en plus du CORS classique (deja present via
  `Access-Control-Allow-Origin: *` dans l'image MapServer). Ajoute via un
  snippet Apache (`mapfiles/apache-pna.conf`, monte dans
  `/etc/apache2/conf-enabled/`).
- **Mixed content sur les tuiles image** : les requetes XHR/fetch (GetCapabilities)
  passent vers `http://localhost`, mais les tuiles WMS chargees comme `<img>`
  sont auto-upgradees par Chrome vers `https://` et echouent silencieusement (pas
  de TLS sur le port local). Solution : un reverse proxy nginx local qui
  termine le TLS avec un certificat `mkcert` (confiance uniquement sur la
  machine locale, pas d'exposition externe) devant MapServer
  (`mapfiles/nginx-tls.conf`, service `mapserver-tls` sur le port 8443 a
  l'origine, passe a 8444 depuis -- voir point 14).

Au final, ces deux points n'etaient PAS la cause du probleme d'affichage (point
2 ci-dessus l'etait) : une fois le HTTPS en place, le symptome etait identique.
Mais les deux restent necessaires pour qu'un client HTTPS public puisse
atteindre le noeud local du tout -- a garder pour tout futur test similaire.

## 5. Le `<MetadataURL>` genere par defaut par MapServer est un lien mort -- RESOLU

Constate en demandant pourquoi un client (MapStore) n'arrivait pas a lire les
metadonnees d'une couche alors que WMS/WFS fonctionnaient. GetCapabilities
annonce bien un `<MetadataURL type="TC211">`, mais :

1. son `href` pointe vers `{wms_onlineresource}/wms?request=GetMetadata&layer=...`
   -- un chemin qui n'existe pas (c'est `?map=RUDI&...` ou `/RUDI/ogcapi/...`,
   jamais `/wms?`) ;
2. meme corrige, `GetMetadata` n'est pas une operation WMS que MapServer
   implemente (`msWMSDispatch(): ... Incomplete or unsupported WMS request`).

Ce `<MetadataURL>` est entierement synthetise par MapServer a la generation
des Capabilities (rien dans le mapfile ne le declare) -- il suffit de le
surcharger explicitement par couche (`wms_metadataurl_href`,
`_format`, `_type`) pour qu'il soit pris en compte a la place du defaut casse.

**Correction** : `write_metadata_xml()` dans `sync.py` genere, pour chaque
couche, une fiche XML minimale (profil ISO 19139/TC211 : titre, resume,
mots-cles, producteur, emprise, lien vers la fiche complete du catalogue
Rudi) a partir des metadonnees deja presentes dans le catalogue. Ces fiches ne
peuvent pas etre servies par le conteneur `mapserver` lui-meme : son image
capte tout le chemin `/` via un `ScriptAliasMatch` global (voir point 1),
aucun fichier statique n'y est atteignable (verifie : meme un `Alias` plus
specifique est ignore par Apache, qui previent explicitement "will probably
never match because it overlaps an earlier ScriptAliasMatch"). D'ou un
service nginx statique dedie, `geo-metadata` (port 8090), qui sert
`generated/metadata/<media_id>.xml`.

A noter en chemin : le lien de retour vers la fiche catalogue doit utiliser
une URL joignable depuis un navigateur (`CATALOG_PUBLIC_URL`), jamais
`CATALOG_URL` (qui vaut `host.docker.internal`, resolu seulement depuis
l'interieur des conteneurs).

## 6. Un meme media peut etre liste plusieurs fois dans une resource du catalogue -- RESOLU

Constate en regenerant les fiches de metadonnees : 581 couches publiees pour
seulement 555 media_id distincts. Une resource du catalogue local listait le
*meme* media_id 14 fois dans ses `available_formats` (donnee du catalogue,
pas un bug de pagination cote synchroniseur -- verifie en interrogeant le
catalogue directement). Consequence : MapServer publiait 14 `LAYER` de meme
`NAME`, ce qui rend les Capabilities WMS invalides (noms de couche en double,
interdits par la norme).

**Correction** : `iter_candidate_media()` deduplique desormais par
`media_id` au fil de l'iteration. A surveiller si le catalogue est corrige en
amont (la deduplication cote synchroniseur restera une garde-fou utile de
toute facon).

## 7. Apres recreation du conteneur `mapserver`, le proxy HTTPS renvoie un 403 qui n'a rien a voir -- RESOLU

En retestant l'acces HTTPS (`https://localhost:8443/...` a l'epoque, point 4) apres
plusieurs cycles de corrections qui ont recree le conteneur `mapserver` (IP
interne changee a chaque fois), le proxy `mapserver-tls` (nginx) a fini par
repondre `403 Forbidden` sur **toute** requete, y compris `GET /` sans aucun
parametre -- alors que `mapserver` lui-meme repondait normalement en direct
(port 8089) et qu'un test fait *depuis l'interieur* du conteneur nginx vers
`http://mapserver:80` fonctionnait. Le 403 portait meme les en-tetes CORS/PNA
attendus, ce qui a egare le diagnostic (on pouvait croire que la requete
atteignait bien l'application). Le journal d'acces d'Apache ne montrait
aucune trace de ces requetes : elles n'arrivaient jamais jusqu'a lui --
nginx repondait seul, avant tout `proxy_pass`.

Cause probable : nginx garde une connexion/route vers l'ancienne IP du
conteneur `mapserver` recree entre-temps ; ce conteneur `nginx` n'avait lui
jamais ete redemarre depuis le debut de la session.

**Correction** : recreer le conteneur `mapserver-tls`
(`docker compose up -d --force-recreate mapserver-tls`) regle le probleme
immediatement. A retenir : **tout conteneur qui fait du `proxy_pass` vers un
nom de service doit etre redemarre apres toute recreation de ce service**,
ou utiliser un `resolver` nginx explicite avec une duree de cache courte pour
re-resoudre le nom en continu plutot qu'une seule fois au demarrage.

## 8. `wms`/`wfs_onlineresource` casses : tout lien auto-construit par MapServer pointait vers une URL fictive -- RESOLU

Constate en testant le WFS depuis le vrai MapStore public : le catalogue
affichait bien la couche, GetFeature marchait en direct (curl), mais ni la
fiche de metadonnees ni la table attributaire ne s'affichaient dans
MapStore, **sans aucun message d'erreur**. Cause : `wms_onlineresource` et
`wfs_onlineresource` valaient `{base_url}/wms?` / `{base_url}/wfs?`, un
chemin de "branding" purement decoratif, jamais fonctionnel (meme probleme
que le `MetadataURL` du point 5 : la ou MapServer utilise cette valeur pour
construire SES PROPRES liens de retour dans le corps des reponses --
`OnlineResource` des Capabilities, `xsi:schemaLocation`, et surtout le lien
de pagination `next` du WFS GetFeature -- ce lien etait mort (`map=RUDI`
absent, chemin inexistant). MapStore semble suivre ce lien plutot que de
reconstruire ses requetes a partir de l'URL saisie par l'utilisateur, d'ou
l'echec silencieux (fetch en arriere-plan qui echoue sans remonter d'erreur
visible).

Deuxieme defaut sur la meme cause : le `<MetadataURL>` du point 5 n'avait
ete corrige que pour le WMS (`wms_metadataurl_*`) -- le WFS genere sa PROPRE
`<MetadataURL>` par `FeatureType` a partir de cles separees
(`wfs_metadataurl_*`), jamais renseignees : un client qui ajoute le WFS
(plutot que le WMS) retombait donc sur l'ancien lien casse.

**Correction** : `wms_onlineresource`/`wfs_onlineresource` valent
desormais `{base_url}/?map=RUDI&` (le `?map=RUDI&` fait partie de la valeur,
MapServer ne l'ajoute pas automatiquement) ; `NODE_PUBLIC_URL` est passee a
`https://localhost:8443` (l'URL HTTPS reellement fonctionnelle, cf. point 4
-- passee a 8444 depuis, voir point 14) au lieu d'un chemin fictif. Et `wfs_metadataurl_href/_format/_type` sont
desormais renseignees en parallele de `wms_metadataurl_*`, vers la meme
fiche XML.

A retenir pour la vraie mise en production : toute cle `*_onlineresource`
doit etre une URL qui, une fois reconstituee avec les parametres que
MapServer lui ajoute, repond reellement -- jamais une adresse de confort
non testee.

## 9. Ajouter le service WFS depuis MapStore : URL a deux `?`, puis format refuse -- RESOLU

Deux bugs enchaines, decouverts en essayant d'ajouter le flux WFS du noeud
dans le vrai MapStore public (apres le WMS, point 2/3).

**1. "Service non disponible" au moment d'ajouter/re-ajouter le catalogue.**
MapStore construit certaines requetes (constate sur `DescribeFeatureType`) en
concatenant betement `url_du_catalogue + "?" + ses_propres_parametres`, sans
verifier si `url_du_catalogue` contient deja un `?...`. Comme l'URL que nous
recommandions etait `.../?map=RUDI&SERVICE=WFS&VERSION=2.0.0&REQUEST=
GetCapabilities`, le resultat avait deux `?`, donc un parametre `REQUEST`
dont la valeur litterale etait `"GetCapabilities?service=WFS"` -- MapServer
ne reconnaissait plus rien et retombait sur son mode `BROWSE` par defaut
(qui echoue, faute de `TEMPLATE`).

**Correction** : `MS_MAPFILE` est ajoute a la config MapServer (`mapfiles/
mapserver.conf`), pour que le mapfile `RUDI` soit utilise *par defaut* quand
aucun `map=` n'est fourni. Le `?map=RUDI&` cesse d'etre necessaire pour
WMS/WFS classiques (OGC API Features, qui route par chemin et pas par
parametre, n'est pas concerne et continue de fonctionner a l'identique).
Ca ne supprime pas le probleme de fond (MapStore peut recreer la meme URL a
deux `?` sur n'importe quelle base), mais une URL plus courte en pratique
limite les occasions.

**2. Une fois le service ajoute, aucune couche ne s'affiche (metadonnees,
table attributaire, visualisation), sans aucun message d'erreur.** Un
`GetFeature` manuel confirmait que le service repondait -- mais avec
l'`OUTPUTFORMAT` exact que MapStore envoie reellement
(`outputFormat=application/json`, observe dans les journaux nginx), la
reponse etait une exception :
```
'application/json' is not a permitted output format for layer '...',
review wfs_getfeature_formatlist setting.
```
Notre mapfile ne declarait que `application/geo+json` (format "officiel"
GeoJSON) ; MapStore demande le mime-type generique `application/json`, un
alias different, jamais couvert.

**Correction** : un second `OUTPUTFORMAT` (`NAME "json"`, `MIMETYPE
"application/json"`, meme driver OGR/GeoJSON) ajoute dans l'en-tete du
mapfile, et `wfs_getfeature_formatlist`/`wfs_getfeature_formats` passes a
`"geojson,json,GML2,GML3"`.

A retenir : **toujours rejouer la requete EXACTE qu'envoie le client reel**
(capturee dans les journaux), jamais une requete "equivalente" ecrite a la
main -- les deux bugs de cette section ont des symptomes identiques
("rien ne s'affiche, pas d'erreur") pour des causes totalement differentes,
et ne se reproduisent qu'avec les parametres precis que MapStore construit.

## 9bis. Le panneau "métadonnées" de MapStore ("Aucune métadonnée disponible") -- RESOLU cote noeud, limitation cote client CONFIRMEE

Capture d'ecran a l'appui : le bouton "i" de MapStore affiche une boite
"Metadonnees de la couche / Aucune metadonnee disponible", generee
instantanement, sans la moindre requete reseau vers notre service de
metadonnees (port 8090 -- confirme par ses journaux, vides). Ce bouton ne
suit donc jamais le `<MetadataURL>` corrige au point 5 : il lit uniquement
les champs **`<Abstract>`** et **`<KeywordList>`** embarques directement
dans le corps du GetCapabilities WMS/WFS -- deux champs que nous ne
renseignions pas du tout (`ows_abstract`/`ows_keywordlist` absents du
mapfile).

**Premiere correction** : `ows_abstract` et `ows_keywordlist` ajoutes par
couche dans `sync.py`, a partir du `summary`/`synopsis` et des `keywords`
deja presents dans le catalogue Rudi (meme source que la fiche XML du
point 5). Confirme present dans les deux Capabilities (WMS et WFS) par
curl. Prefixe `ows_` (et non `wms_`/`wfs_` separement) : ce prefixe
generique couvre les deux protocoles a la fois dans MapServer, comme
`ows_title` l'illustrait deja.

**Mais le panneau "i" reste vide meme apres**, y compris apres un
 re-ajout complet du service WFS. La console du navigateur (outils de
dev **de l'onglet**, pas la "Browser Console" de Firefox qui debogue le
navigateur lui-meme -- piege dans lequel on est tombe une fois) confirme :
aucune requete reseau n'est emise au moment du clic sur "i". Les
metadonnees affichees (ou absentes) viennent donc d'un etat deja en
memoire cote client, pas d'une lecture a la demande.

Hypothese, etayee par ce qu'on voit par ailleurs dans la meme console :
MapStore emet, a chaque interaction avec le catalogue, une requete de
"validation" qui mélange les parametres par defaut d'un WMS
(`service=WMS&version=1.3.0&request=GetCapabilities`) ET ceux du service
reellement enregistre (`SERVICE=WFS&VERSION=2.0.0`), sans jamais retirer
les premiers -- reproduit textuellement :
```
?service=WMS&version=1.3.0&request=GetCapabilities&SERVICE=WFS&VERSION=2.0.0&
```
Rejouee telle quelle, cette requete echoue systematiquement cote serveur
(`mapserv(): ... Traditional BROWSE mode requires a TEMPLATE`), quel que
soit l'etat du mapfile -- SERVICE/VERSION s'y trouvent chacun en double,
avec une casse differente et des valeurs contradictoires (WMS vs WFS),
qu'aucun serveur OGC ne peut arbitrer correctement. Si c'est cette
requete-la qui alimente (ou est censee alimenter) le cache de
metadonnees du catalogue, le probleme est **cote MapStore**, pas
cote noeud : aucune configuration serveur ne peut rendre coherente une
requete qui porte deux valeurs contradictoires pour le meme parametre.

**Confirme le 2026-10-09** : le WFS officiel de la Geoplateforme IGN
(`https://data.geopf.fr/wfs/ows`), ajoute dans la meme instance MapStore,
affiche exactement le meme symptome -- "Aucune metadonnee disponible" et
"Aucune fonctionnalite disponible" / "aucun objet" dans la table. Un
service de reference, par ailleurs reconnu conforme, produit le meme
echec : **ce n'est donc pas un bug du noeud pilote, mais une limitation
(ou un bug) de cette instance MapStore elle-meme**, qui touche
indistinctement n'importe quel service WFS. Rien a corriger cote pilote
au-dela des points deja traites plus haut (Abstract/KeywordList,
formats, pagination) -- ils restent necessaires et corrects, seulement
pas suffisants pour ce client particulier.

A retenir malgre tout : **chaque client peut lire les metadonnees d'une
facon differente** (certains suivent `<MetadataURL>`, d'autres affichent
`<Abstract>`/`<KeywordList>`) -- les deux doivent etre renseignes pour
couvrir large, meme si ça ne suffit pas toujours a satisfaire un client
par ailleurs bogue.

## 10. WFS GetFeature sans `COUNT` renvoyait TOUTES les lignes -- RESOLU

Decouvert en ajoutant la couche "arbres" (`referentiel-arbre-namr-csv`,
122 747 lignes, 10 Mo en CSV source) dans MapStore : la couche s'affiche
bien en WMS (rendu cote serveur, taille d'image independante du nombre de
lignes), mais le GetFeature WFS que MapStore envoie pour la table
attributaire/la visualisation vectorielle **ne precise aucun `COUNT`** --
et MapServer, conforme a la norme WFS 2.0 (silence = "tout renvoyer"),
retournait l'integralite de la couche : **52 Mo de JSON, 122 747 features
en une seule reponse**, confirme par un avertissement nginx ("upstream
response is buffered to a temporary file"). De quoi expliquer un
navigateur qui ne finit jamais de parser la reponse, sans le moindre
message d'erreur.

**Correction** : `wfs_maxfeatures "5000"` dans les metadonnees WEB du
mapfile (niveau MAP, PAS niveau LAYER -- teste et confirme : la meme cle
posee par couche est silencieusement ignoree). Plafonne toute reponse
GetFeature a 5000 entites meme sans `COUNT` explicite, y compris quand le
client en demande plus. A ajuster si une pagination cote client (plusieurs
appels avec `STARTINDEX`) est prevue pour les tres gros jeux de donnees.

## 10bis. Declaration des medias SERVICE : plus simple en pratique que prevu par la note

La note anticipait un mecanisme lourd (API privee du catalogue, JWT signe
avec une cle SSH). En pratique :

- Il existe deja une librairie Python mure pour cet usage,
  [`rudi-node-write`](https://pypi.org/project/rudi-node-write/), qui gere
  toute la complexite d'authentification en interne.
- Elle ne parle **pas directement au catalogue** mais au module **manager**
  du noeud (port 4032), avec une authentification **utilisateur/mot de
  passe** (`admin`/`admin` sur un noeud de dev) -- le manager se charge
  lui-meme de forger le JWT (signe EdDSA avec une cle SSH ed25519,
  `source-keys/catalog_mngr`) pour parler au catalogue en notre nom.
  Aucun JWT a manipuler soi-meme.
- Sur ce noeud de dev, la verification des requetes privees est de toute
  facon desactivee (`should_control_private_requests = false` dans la
  config catalogue) -- le JWT ne serait meme pas controle. A verifier avant
  tout passage en quasi-production : la vraie API privee (`/catalog/admin/
  ...`) exigera alors un JWT valide, ce que `rudi-node-write` continuera de
  gerer de toute facon via le manager.
- Ecriture = remplacement complet de la resource (`PUT /admin/resources`,
  pas un patch) : il faut donc toujours relire la resource existante avant
  d'y ajouter un media, jamais construire l'objet de toutes pieces (risque
  d'ecraser des champs qu'on n'a pas touches).

Voir `declare_services.py` : implemente et teste sur les 117 ressources
du noeud local ayant au moins une couche publiee par ce pilote.

## 10ter. Conversion FlatGeobuf des gros fichiers : `-overwrite` incompatible avec un format mono-couche -- RESOLU

En ajoutant la conversion automatique des fichiers volumineux (>5 Mo) en
FlatGeobuf (indexe spatialement, voir README.md) via `ogr2ogr -overwrite`,
premiere execution en echec silencieux (capture seulement le code retour,
pas la sortie) :
```
ERROR 6: DeleteLayer() not supported by this dataset.
ERROR 1: DeleteLayer() failed when overwrite requested.
```
`-overwrite` tente de supprimer la couche existante avant d'ecrire --
`DeleteLayer()` n'a pas de sens pour un format **mono-couche** comme
FlatGeobuf (un fichier = une couche, rien a "supprimer a l'interieur").
Fonctionne sans probleme sur un format multi-couches (GeoPackage...), d'ou
la confusion initiale (c'est le pattern habituel partout ailleurs dans ce
pilote).

**Correction** : supprimer le fichier de sortie nous-memes
(`fgb_path.unlink(missing_ok=True)`) avant l'appel, sans `-overwrite`.

A retenir : capturer `stderr` (pas seulement le code retour) des l'ecriture
d'un appel `subprocess` a un outil GDAL -- le message d'erreur reel aurait
evite un aller-retour de diagnostic.

## 11. GetFeatureInfo en `INFO_FORMAT=application/json` echoue

`msDrawMap(): Image handling error. Failed to draw layer` -- le format GML
(`application/vnd.ogc.gml`) fonctionne parfaitement et porte les memes attributs.
A reproduire avec une image MapServer plus recente / un rapport en amont avant de
s'appuyer sur le JSON pour GetFeatureInfo.

## 12. Deux reglages de mapfile non documentes par la note initiale, decouverts en pratique

- Les couches OGR doivent utiliser `CONNECTION "<fichier>"` + `DATA "<nom_de_couche>"`
  (pas `DATA "<fichier>"` seul) : sinon `msOGRFileOpen(): Error parsing OGR connection
  information`, sur 100% des couches testees.
- La metadata pour la liste des formats WFS s'appelle `wfs_getfeature_formatlist`
  et non `wfs_getfeature_formats` (le message d'erreur de MapServer le dit
  explicitement : "review wfs_getfeature_formatlist setting").
- Une couche n'est marquee "queryable" dans les Capabilities (et donc utilisable en
  GetFeatureInfo) que si elle porte un `TEMPLATE` -- meme un nom de fichier qui
  n'existe pas sur disque suffit, mais l'attribut doit etre present.

## 13. Geometries heterogenes ignorees

144 medias GeoJSON sur les 1257 candidats sont ignores car OGR rapporte un type de
geometrie generique `Geometry` (FeatureCollection a geometries melangees). Confirme
le point "limites" de la note : extension possible plus tard (scinder par type de
geometrie), hors scope du pilote.

## 14. Relais via le portail (`/medias/.../wms`) : redirection `/login` trompeuse -- cause identifiee, pas resolue en local

En ajoutant des medias `SERVICE` pointant vers nos propres flux (point 10bis)
sur une resource deja publiee sur le portail, le mecanisme PUT/update cote
catalogue fonctionne (confirme par les logs `kalim`/`apigateway` : la route
est bien creee en base, `integration_status: OK`). Mais l'acces reel au flux
*via le relais du portail* (`https://rudi.localhost/medias/<global_id>/
<media_id>/wms?...`) echouait systematiquement par un `302 Found` vers
`/login?error` -- jamais une erreur explicite, jamais vu comme une
authentification refusee (teste avec un token anonyme valide, meme
resultat), ni comme un probleme de contrat d'URL (`RerouteToRequestUrlFilter`
loggue des "Parameter does not exists in contract" mais ne bloque rien,
verifie en lisant son code).

**Cause reellement identifiee** (en instrumentant temporairement
`AnonymousRemoteWebFilter` cote source, rudi-portal-source, pour tracer
chaque invocation -- patch jete apres diagnostic, jamais commite) : le
service `apigateway` du portail ecoute lui-meme sur le port 8443 en interne
(son propre serveur embarque). Quand il relaie la requete vers notre
`connector.url` (`https://localhost:8443/...`), ce "localhost" est resolu
DEPUIS LE CONTENEUR `apigateway` lui-meme (reseau bridge Docker
`rudiplatform_default`, jamais de `network_mode: host`) -- donc vers
`apigateway` lui-meme, jamais vers `mapserver-tls`. La requete boucle
silencieusement sur l'app apigateway elle-meme, qui ne reconnait pas le
chemin racine `/` (notre URL n'a pas de chemin, tout est en query string) et
redirige vers `/login`, comme n'importe quelle requete web non authentifiee
sur un chemin non public. Confirme definitivement : zero connexion recue sur
`mapserver-tls` pendant ces tests (`docker logs rudi_geo_mapserver_tls`
vide), alors qu'un flux externe pre-existant (ex. data.geopf.fr, un vrai nom
d'hote public) fonctionne parfaitement via ce meme relais.

Ce n'est donc ni un bug RUDI, ni une lacune structurelle de la synchronisation
metadonnee noeud -> portail (celle-ci fonctionne). C'est un artefact pur de
topologie locale : deux services independants (`mapserver-tls` du pilote,
`apigateway` du portail) qui choisissent, par hasard, le meme port (8443) sur
la meme machine, et un conteneur qui ne peut jamais, par construction
Docker, atteindre un AUTRE conteneur ni l'hote via `localhost`.

**Correction partielle appliquee** : port de `mapserver-tls` change de 8443 a
8444 (`docker-compose.geo.yml`, `declare_services.py`, `sync.py`,
`NODE_PUBLIC_URL`). Effet reel : la fausse redirection `/login` (qui a fait
perdre un temps a croire a un probleme d'authentification) est remplacee par
une connexion refusee franche -- un diagnostic honnete, mais **le relais via
le portail ne fonctionne toujours pas en local**, puisque le probleme de fond
(`localhost` jamais joignable entre conteneurs) n'est pas resolu par un
simple changement de port. Pas de correction plus poussee en local : en
production, le noeud a un domaine public reel et distinct, ce qui fait
disparaitre entierement cette ambiguite -- ce n'est donc pas une limite a
traiter avant mise en prod, seulement un inconfort de cet environnement de
dev. Pour tester reellement le relais en local, il faudrait soit joindre
`mapserver-tls` au reseau Docker du portail et utiliser son nom de service
comme hote (a tester : passerait-il le validateur stricte d'URL du
catalogue ? non verifie), soit accepter l'acces direct au noeud (sans passer
par le portail) comme seul chemin fonctionnel en dev local -- ce qui reste
de toute facon l'usage prevu pour MapStore pointant directement sur le
noeud (voir la note d'architecture, option A).

## 15. Un seul media WMS/WFS generique pour toute une resource multi-couches : illisible -- RESOLU

Le premier jet de `declare_services.py` creait un seul media
`service-wms-noeud`/`service-wfs-noeud` par resource, pointant vers l'URL
`GetCapabilities` generique du noeud -- identique pour les 581 couches
publiees. Pour une resource a une seule couche (cas majoritaire), rien de
grave : le client n'a qu'une couche a trouver dans les Capabilities. Mais
pour les 68 resources multi-couches (sur 117), ce media generique ne permet
pas de savoir a quelle couche il correspond -- un client doit deviner ou
parcourir toutes les couches du noeud.

**Pratique SIG/SDI standard confirmee** (ISO 19139/INSPIRE "view service") :
un `GetCapabilities` WMS/WFS ne se filtre pas par couche (pas prevu par la
norme OGC -- contrairement a OGC API Features, adressable par collection).
L'URL de service reste donc generique, mais la couche precise est portee
par un parametre structure separe, `connector_parameters` / `layer`, que les
clients SIG (QGIS, MapStore...) utilisent pour preselectionner la bonne
couche. Confirme par un exemple deja present dans ce meme catalogue (flux
externe IGN, media `service-wfs` : `connector_parameters` contient
`layer: "OCS-GERS_BDD_LAMB93_2016:oscge_gers_32_2016"`, aux cotes de
`versions`, `default_crs`, `formats`).

**Tentatives, dans l'ordre, et pourquoi elles ont ete abandonnees** :

1. Un media par couche et par protocole (`service-wms-noeud__<layer>`),
   chacun avec son URL generique + `connector_parameters.layer`. Insuffisant
   en pratique : aucune interface consultee (Manager du noeud) n'affiche ce
   parametre -- deux medias WMS de couches differentes, meme `url`
   generique affichee, restent visuellement et fonctionnellement
   indiscernables pour quiconque regarde juste le lien (confirme : sur une
   resource a 2 couches, les 2 liens WMS -- et les 2 liens WFS -- rendus
   comme strictement identiques dans l'ecran "modifier" du Manager, HTML
   source inclus). Un media distinct en base ne suffit pas si le lien
   montre a l'utilisateur ne l'est pas.
2. Meme principe, mais avec le nom de couche ajoute directement dans l'URL
   affichee (`&LAYERS=<layer>` pour WMS, `&TYPENAMES=<layer>` pour WFS --
   confirme sans effet de bord, MapServer les ignore silencieusement sur une
   requete `GetCapabilities`, ni norme OGC ni erreur). Le lien devenait
   visiblement distinct, mais **insuffisant aussi** : teste avec de vrais
   clients (MapStore, un explorateur WFS independant), aucun ne pre-selectionne
   la couche -- limite du protocole lui-meme, pas de notre implementation.
   `GetCapabilities` renvoie *toujours* le document complet listant toutes
   les couches du service, quel que soit ce qu'on ajoute dans l'URL ; aucun
   mecanisme standard ne permet a un client generique de filtrer a partir de
   la, que ce soit via un parametre d'URL ou `connector_parameters`. Vrai
   pour n'importe quel WMS/WFS au monde (ajouter `data.geopf.fr/wfs/ows`,
   l'exemple externe deja dans ce catalogue, dans MapStore afficherait
   aussi *toutes* les couches IGN).

**Decision finale** : WMS et WFS restent **toujours generiques** (1 seul
media par protocole et par resource, quel que soit le nombre de couches,
sans `LAYERS`/`TYPENAMES` ni `connector_parameters`) -- la distinction par
couche n'a aucun effet pratique pour ces deux protocoles, mieux vaut ne pas
la simuler. **OGC API Features reste la seule exception** : son URL pointe
reellement vers la collection precise (chaque collection a son propre
endpoint REST fonctionnel, contrairement a WMS/WFS) -- un media par couche
y garde donc un sens reel, dans la limite de `LAYER_MEDIA_THRESHOLD`
couches (20 par defaut, `RUDI_GEO_LAYER_MEDIA_THRESHOLD` ; au-dela, repli
sur la liste generale des collections -- rencontre en pratique sur une
resource a 84 couches, qui faisait aussi echouer le `PUT`, `request entity
too large`, avant ce repli). Si la forme change (resource qui gagne/perd
une couche), les medias de l'ancienne forme sont detectes (prefixe
`service-<protocole>-noeud`) et supprimes plutot que laisses perimes a cote
des nouveaux.

## 16. Declaration autonome des services depuis `geo-sync` : couche pip dediee et cadence -- a reporter dans la note d'architecture

Depuis l'extraction du pilote en depot autonome `rudi-mapserver`, le conteneur
`geo-sync` declare lui-meme les medias SERVICE apres chaque cycle ou le jeu de
couches publiees change (`RUDI_GEO_SELF_DECLARE`, actif par defaut) : un simple
`docker compose up` suffit alors, sans pipeline externe. Deux points a signaler
dans la note :

- **Couche pip dediee** : l'image `camptocamp/mapserver:8.6.0` est une Ubuntu
  24.04 sans pip (`python3 -m pip` -> `No module named pip`) et "externally
  managed" (PEP 668) ; `Dockerfile.geo-sync` installe donc `python3-pip` puis
  `pip install --break-system-packages rudi-node-write`. C'est le seul ecart a
  l'image MapServer d'origine ; `sync.py` et `declare_services.py` restent
  montes en bind `:ro` (edition sans reconstruction), seul un changement de
  dependance pip impose `--build`.
- **Cadence** : la declaration n'est declenchee que si le hash du contenu trie
  de `resources_with_layers` change (pas toutes les 60s sur un catalogue
  inchange), et jamais au prix de la boucle de sync (try/except, warning en cas
  d'echec, retente au cycle suivant). L'appel deja existant depuis
  `moissonneuse-batteuse` (cote hote) reste inchange et cohabite sans conflit :
  `declare_services.py` est idempotent.

## Resultats de la recette automatique (`test_services.py`)

8 / 8 controles passes contre les vraies donnees du noeud source local (581 couches
publiees sur 1257 medias candidats, 396 resources) : WMS GetCapabilities/GetMap/
GetFeatureInfo (GML), WFS GetFeature (GeoJSON, EPSG:3857) et OGC API Features
natif (`/RUDI/ogcapi/collections`, `.../items`) sont tous verifies, avec un seul
conteneur MapServer (plus le synchroniseur) -- aucune brique supplementaire.

**Test en conditions reelles (2026-10-09)** : une couche WMS du noeud ajoutee et
affichee avec succes dans le MapStore public de Rennes Metropole
(`https://public.sig.rennesmetropole.fr/mapstore/#/`), exactement le client vise
par la note. Premier vrai client externe, hors recette automatisee. Deux bugs
reels trouves et corriges par ce seul test, invisibles a la recette automatique
(qui ne verifie qu'une couche, generalement polygonale) : noms de couche
invalides (point 2) et symbole ponctuel non defini (point 3) -- preuve que
tester contre un vrai client, sur plusieurs familles de couches, est
indispensable avant toute mise en production.
