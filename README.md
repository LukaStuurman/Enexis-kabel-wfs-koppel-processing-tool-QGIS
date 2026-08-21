# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Nieuw in v0.12.0: landelijke WFS-CSV-koppeling op schijf

Laat **Beperk WFS tot scherm/gebied** leeg om heel Nederland te verwerken. De
plugin gebruikt dan niet langer de oude route die alle volledige CSV-rijen in
RAM hield en voor ieder groepje CSV-labels een aparte landelijke WFS-filter
uitvoerde.

De landelijke route werkt nu als volgt:

1. de CSV wordt één keer naar een tijdelijke SQLite-index op een lokale schijf
   gestreamd;
2. de WFS wordt één keer paginagewijs in stabiele `fid`-volgorde gelezen, met
   alleen `label`, `fid` en geometrie;
3. alleen WFS-kabels waarvan het label ook in de CSV staat blijven in de
   tijdelijke schijfcache;
4. na de download worden CSV en WFS per maximaal 50 gezamenlijke labels vanaf
   schijf geladen;
5. binnen ieder label blijft de bestaande strikte 1-op-1 lengtematching gelden;
6. gekoppelde lijnen en niet-gekoppelde CSV-rijen worden rechtstreeks naar de
   gekozen uitvoer geschreven;
7. de tijdelijke SQLite-cache wordt altijd gesloten en verwijderd.

De WFS-pagina bevat maximaal 10.000 objecten en maximaal 64 MB. Tijdelijke
netwerk- en serverfouten worden maximaal drie keer opnieuw geprobeerd.

### Gemeten met de meegeleverde landelijke CSV

De CSV van 18 augustus 2026 is 504.444.333 bytes groot en bevat 1.992.366
rijen en 666.693 geldige unieke labels. Het bouwen van de CSV-schijfindex duurde
in de lokale QGIS 4.2-test circa 32 seconden. De index was 693 MB. Eén echte,
stabiel gesorteerde WFS-pagina bevatte 10.000 van in totaal 1.990.383
kabeldelen; 9.292 daarvan hadden een label dat ook in de CSV voorkwam. De
gemeten piek van het volledige testproces was circa 316 MB RAM.

Voor een landelijke uitvoering:

- kies voor beide uitvoerlagen een **GeoPackage op lokale SSD**, geen tijdelijke
  geheugenlaag;
- kies bij **Landelijke modus: tijdelijke cachemap** eveneens een lokale SSD;
- houd minimaal 5 GB vrije schijfruimte beschikbaar;
- reken door de bijna twee miljoen WFS-geometrieën op tientallen minuten of
  langer, afhankelijk van GeoServer en de verbinding.

## Nieuw in v0.11.0: landelijke DXF-streaming

Voor een export van heel Nederland heeft **Split gekoppelde kabels naar DXF
(V6 - landelijk)** een aparte streamingmodus. Schakel **Landelijke
streamingmodus** in om:

- alle kabels uit de invoerlaag zonder selectie of zoekradius te verwerken;
- standaard alleen objecten met `match_status = GEKOPPELD` te exporteren;
- alleen geometrie, `wfs_label_norm`, `csv_Type` en `match_status` bij de
  bronprovider op te vragen;
- iedere kabel direct naar DXF te schrijven, zonder landelijke
  geometrieverzameling in RAM;
- automatisch een nieuw bestand `Nederland_Kabels_0001.dxf`,
  `Nederland_Kabels_0002.dxf`, enzovoort te starten;
- het maximale aantal kabels per DXF-deel in te stellen (standaard 25.000).

Lijnen samenvoegen wordt in de landelijke modus bewust overgeslagen. Dat zou
alle geometrieën per laagnaam opnieuw in het geheugen moeten verzamelen en het
belangrijkste schaalvoordeel tenietdoen. Kleuren worden met een stabiele hash
per projectcode gekozen, zodat dezelfde projectcode in ieder DXF-deel dezelfde
kleur houdt.

## Nieuw in v0.10.0: gekoppelde kabels naar DXF

De plugin bevat nu naast de WFS-CSV-koppeling ook **Split gekoppelde kabels
naar DXF (V6 - landelijk)**. Deze tool werkt rechtstreeks met de uitvoerlaag van de
koppeltool:

- standaard labelveld `wfs_label_norm`;
- standaard kabeltypeveld `csv_Type`;
- herkent een gekoppeld label zoals `BEK4020-04` en haalt daar projectcode
  `BEK4020` uit;
- ondersteunt als terugval ook oudere velden zoals `label`, `KabelType` en
  labels met de prefix `Kabelgroup:`;
- beperkt de scan tot de bounding box van de selectie plus een instelbare
  zoekradius;
- kan alles in één DXF schrijven of één DXF per projectcode maken.

Gebruik: selecteer in `Gekoppelde Enexis WFS-lijnen` één of meer kabels en open
**Processing → Toolbox → Enexis → Kabelkoppeling → Split gekoppelde kabels naar
DXF (V6 - landelijk)**.

## Gebruik v0.9.0

Versie **0.9.0** houdt een landelijke CSV in extentmodus niet meer volledig in
het geheugen. De tool scant eerst de WFS-labels in het kaartvenster en streamt
daarna de CSV één keer. Alleen CSV-rijen met een label dat werkelijk in die
extent voorkomt worden bewaard en eventueel als niet-gekoppeld uitgevoerd.
Rijen voor de rest van Nederland komen in extentmodus dus niet meer in de
uitvoer `Niet-gekoppelde CSV-rijen`.

Hierdoor bepalen de omvang van de gekozen extent en het aantal relevante labels
het geheugengebruik, niet langer het totale aantal landelijke CSV-rijen.

## Eerdere verbetering in v0.8.0

Versie **0.8.0** vervangt de oude extent-first aanpak waarbij eerst alle volledige kabelgeometrieën in het scherm werden opgehaald. Dat kon traag zijn en bovendien matches missen wanneer de extent meer kabeldelen bevatte dan de veiligheidslimiet.

De nieuwe extentmodus werkt in twee stappen:

1. de plugin vraagt binnen de gekozen schermextent **alleen het WFS-labelattribuut** op;
2. die WFS-labels worden lokaal genormaliseerd en vergeleken met CSV `Kabel Subgroep`;
3. alleen labels die zowel in de WFS-extent als in de CSV voorkomen krijgen daarna een geometrie-opvraag;
4. alleen die relevante geometrieën worden lokaal opnieuw tot de gekozen extent beperkt;
5. daarna volgt de strikte 1-op-1 lengtematching.

Dus praktisch:

**schermextent → alleen labels ophalen → labels met CSV kruisen → alleen relevante geometrie ophalen → 1-op-1 matchen**

GeoServer ondersteunt `propertyName` in WFS GetFeature om een response tot één attribuut te beperken. Hierdoor hoeft de eerste extent-scan geen kabelgeometrieën of overige attributen te downloaden.

## Waarom dit sneller moet zijn

Een schermextent kan honderden of duizenden kabeldelen bevatten. In v0.7.0 werden daarvan volledige geometrieën opgehaald voordat bekend was of hun labels überhaupt in de CSV voorkwamen.

In v0.8.0 is de eerste response veel kleiner:

- maximaal één WFS-request tegelijk;
- eerste extent-scan bevat alleen het gedetecteerde labelveld;
- maximaal 10 overeenkomende kabelgroepen per geometrie-request;
- geometrie wordt alleen voor daadwerkelijke WFS/CSV-labelovereenkomsten opgehaald;
- geen live `QgsVectorLayer` WFS-provider;
- geen parallelle HTTP-downloads;
- ruwe batches worden na verwerking vrijgegeven.

## Betere diagnose bij 0 matches

De plugin gaat niet meer blind uit van een veld dat exact `label` heet.

Eerst wordt één WFS-feature bekeken. Het labelveld wordt gedetecteerd op:

- exacte veldnaam `label`;
- een veldnaam die `label` bevat;
- herkenbare kabelgroep-veldnamen;
- als laatste fallback een propertywaarde die begint met `Kabelgroup:`.

Bij **0 exacte labelovereenkomsten** schrijft het Processing-log voorbeelden van:

- genormaliseerde WFS-labels uit de extent;
- labels uit CSV `Kabel Subgroep`.

Hiermee is direct zichtbaar of bijvoorbeeld de verkeerde WFS-property, een andere prefix of een andere labelnotatie wordt gebruikt.

## Schermextent

Kies bij **Beperk WFS tot scherm/gebied** bij voorkeur **Use current map canvas extent**.

De extent wordt naar **EPSG:28992 (RD New)** omgerekend en rechtstreeks als WFS `bbox` verstuurd.

Voor de labelscan worden maximaal **10.000 kabeldelen** geaccepteerd, omdat daar alleen één stringattribuut per feature wordt opgehaald. De labelresponse is daarnaast begrensd op **4 MB**. Wordt die grens geraakt, zoom dan verder in.

## Geometrie-opvraag

Na de labelscan wordt de doorsnede bepaald:

`WFS-labels binnen extent ∩ CSV Kabel Subgroep`

Alleen voor deze labels wordt geometrie opgevraagd. In extentmodus combineert de
plugin de ruimtelijke begrenzing en het labelfilter in één CQL-expressie:

`BBOX(geografischeligging, ...) AND label IN (...)`

De Enexis GeoServer accepteert voor deze laag geen losse `bbox` en `cql_filter`
in dezelfde request. De gecombineerde CQL-vorm voorkomt toch een landelijke
labelscan. De geometry-response is begrensd op **8 MB** en maximaal **1.000
features per batch**.

Omdat de tweede request op label werkt, wordt iedere teruggekomen geometrie lokaal opnieuw tegen de gekozen extent gecontroleerd. Een eventueel gelijk label elders kan daardoor niet in de match terechtkomen.

## CSV

De CSV moet minimaal bevatten:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Lengtes als `195`, `16,5`, `196,11` en `196.11` worden ondersteund.

## Koppelregels

1. `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Daarna moet het label **exact** gelijk zijn aan CSV `Kabel Subgroep`.
3. De WFS-lengte wordt als kaartlengte in RD New berekend en op 2 decimalen afgerond.
4. Binnen dezelfde exacte Kabel Subgroep wordt strikt 1-op-1 gematcht op de best passende lengte.
5. Iedere WFS-lijn en iedere CSV-rij wordt maximaal één keer gebruikt.
6. CSV-regels waarvoor binnen de gekozen extent geen gelijk label is gevonden krijgen `GEEN_MATCH_BINNEN_EXTENT`.

## Zonder extent

Zonder extent worden de CSV-labels rechtstreeks in kleine WFS-geometriebatches opgevraagd. Voor normaal interactief gebruik wordt de schermextent aanbevolen.

## QGIS-versie

De plugin is gericht op **QGIS 4.2.0 / Qt6** en gebruikt de QGIS 4 API.

## Installatie / testen

1. Sluit QGIS als een oudere pluginversie eerder is vastgelopen.
2. Start QGIS 4.2.0 opnieuw.
3. Verwijder de oude pluginversie.
4. Installeer de nieuwste repository-ZIP.
5. Controleer dat **versie 0.12.0** actief is.
6. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel Enexis WFS-kabels aan CSV (snelle extent-scan)**.
7. Kies de CSV en **Use current map canvas extent**.
8. Kijk bij een resultaat met 0 matches in het Processing-log naar `WFS-voorbeeld` en `CSV-voorbeeld`.
