# Jellyfin media path namespaces

`PV_JELLYFIN_MEDIA_PATH_MAP_JSON` is optional. Leave the key **absent** if Personal Vault and Jellyfin see identical paths. Lookup uses the literal authorized PV path, preserving existing installations. It never infers a translation; different provider paths fail lookup as before.

When the services mount the same media under different filesystem namespaces, configure the backend with an explicit JSON array:

```json
[
  {"section":"movies","pv_root":"/srv/personal-vault/media/movies","jellyfin_root":"/media/movies"},
  {"section":"tv","pv_root":"/srv/personal-vault/media/tv","jellyfin_root":"/media/tv"}
]
```

`section` must be `movies` or `tv`. `pv_root` is an absolute backend filesystem root; `jellyfin_root` is an absolute POSIX path as reported by Jellyfin. The relative file path beneath the selected root is preserved. These settings translate lookup paths only; they grant no asset permission and do not move media.

A configured key, including an empty string, must validate completely. Missing fields, unknown sections, traversal, unsafe provider syntax, no matching root, or multiple matching roots fail closed. Authoritative PV placement and audience checks happen before lookup. Root resolution cannot authorize an escaped file. Do not supply overlapping mappings for a section.

Put this JSON in the backend runtime environment, such as `config/auth.env`, using the actual paths visible to each service. Do not put an empty fallback for this variable into Compose: presence changes the contract. Existing explicit mappings keep the same validation and playback behavior.
