"""
Identity aliases: the same real-world team/club appearing under more than one
source_id in England Hockey's system.

This happens when a club renames or re-registers - the source issues a brand
new club AND team entity with no link to the old one (e.g. "Exe HC" in 2022-23
became "Exeter HC" from 2023-24; nothing in the API connects them). Left alone,
that breaks season-to-season continuity: Elo treats them as two teams, and any
"this club over the years" analysis splits in two.

Maps OLD source_id -> CANONICAL source_id. Applied in two places:
  1. loaders/mysql.py rewrites references on every ingest, so re-ingesting an
     old season can never resurrect the stale identity.
  2. `ingest apply-aliases` merges rows already in the DB.

These are human-confirmed facts, not guesses - name similarity alone is NOT
enough (Chelmsford/Cheltenham share a prefix and are different clubs). Only
add an entry once someone who knows the league has confirmed it's the same
side. Include the reasoning in a comment.
"""

# team source_id (old) -> team source_id (canonical)
TEAM_ALIASES: dict[str, str] = {
    # "Exe 1" (Women's Conference West 2022-23) -> "Exeter 1" (2023-24 onward).
    # Same side, club renamed Exe HC -> Exeter HC; finished 3rd in the same
    # league both seasons. Confirmed by the user 2026-09-17.
    "23db3782-5a7a-4e0b-af9e-dc7a9bc255af": "5f160d0a-8668-4dad-9cdb-c413812d8434",
    # "Isca & University of Exeter 1" (2022-24, under Isca HC) -> "University of
    # Exeter W1" (2024-25 onward). A joint side formed in 2019 when Isca's women
    # joined the university club; promoted to the Women's Prem for 2023-24 and
    # relegated. The partnership then ended: the national-league place stayed
    # with the university (host club), which reappears in exactly the league a
    # relegated side lands in (Women's Division 1 South); Isca's women dropped to
    # regional hockey. TEAM alias only - Isca HC is a real, ongoing club with its
    # own men's side, so no club alias. Confirmed by the user 2026-09-17.
    "8541afbf-8ad8-46f9-af7c-6c10fbee4788": "3d377861-033c-4b87-88f7-67e9c4c04bf2",
}

# club source_id (old) -> club source_id (canonical)
CLUB_ALIASES: dict[str, str] = {
    # Exe HC -> Exeter HC (see TEAM_ALIASES).
    "24c4aa90-51a0-4956-9dde-0692ee8ff9e3": "3acdd435-3c12-408a-b2db-214b88f96939",
}

# player source_id (old) -> player source_id (canonical)
#
# Same person under two ids. The usual cause is a second data source spelling
# the name differently (the Finals Weekend importer creates
# "altius:player:<id>" rows when it can't safely match a league player);
# `dedupe-players` only merges exact same-name-same-club rows, so anything
# spelt differently needs a human to confirm it and land here.
PLAYER_ALIASES: dict[str, str] = {
    # "DREW Victoria" as Altius lists her in the 2026 women's Finals Weekend
    # (Hampstead & Westminster) -> Victoria Drews, the league API's row at the
    # same club. Name mangled on Altius. Confirmed by the user 2026-09-18.
    "altius:player:60214": "0bda4b5c-55f8-48e1-8ccf-5947cac2af0d",
    # Same person registered twice at the same club under a nickname / fuller
    # first name (the league API issues a fresh id each registration). Found
    # by scanning same-club same-surname pairs with the finals importer's
    # nickname matcher, 2026-09-18; every pair had the same team history.
    # Canonical = the row with the longer history. Old -> canonical:
    "bb3fea4b-4ab6-4c57-bf05-ccdf3ac65ade": "75f26072-50a8-43ca-b1c0-66f4410e41c0",  # Joshua -> Josh Lang, Brooklands
    "6d399475-d235-4ca0-9e40-50756c60e6ec": "5b2682cf-cc33-4b03-81f7-9586dac5df30",  # Christopher -> Chris Borsoi, Brighton & Hove
    "6b4b85a5-8b1d-467e-9010-88926fb57883": "f95579da-2ccd-44b4-b4dc-9921e97c22ba",  # "Matilda (Tilly)" -> Matilda Matley, Cheltenham
    "c84d32bb-294a-4bae-80f8-7a9511d38182": "c8c8fd88-4059-4095-b6af-7793147b3838",  # William -> Will Hearne, Doncaster
    "f70f5617-9366-49e0-ab39-22cf8115748a": "c66783a3-c2ae-40cc-8b44-a11159ff91fc",  # Chris -> Christopher Davey, Fareham
    "12cc9087-56fb-4485-bf60-d5c6586229c8": "4cd281bb-4efa-45c4-bcd7-ac2c50bc88ea",  # Samuel -> Sam Watson, Havant
    "5d624b5d-ff94-4cdc-9c8c-ec0121bf5b6b": "612b5fa3-fd3f-4083-861a-9c1463de12f4",  # Steph -> Stephanie Hale, Leicester City
    "582c700b-51ae-4285-9130-4eff88786b76": "7bd82029-3d22-4f5c-92ef-c4ba68792768",  # "Yasmin (Yazi)" -> Yasmin Chahal, Old Georgians
    "e93520c2-23da-460a-8ceb-dfae22fe2b2f": "58b9036a-89e3-469d-a29c-f61233261846",  # Jonathan -> Jonny Parke, Spencer
    "81142262-b05e-4cd3-849f-f0900e252b87": "b5d8dd12-eb90-4c5d-b210-1ece700c883e",  # Sam -> Samuel Parkes, University of Exeter
    "b7a70921-9ea4-41cf-806b-e6af32da3729": "f8fc7519-eecf-4d14-bdac-a29a4fb0114f",  # "Henrietta Katie" -> Henrietta Gibson, Wakefield
}
