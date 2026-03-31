# Soumission des icônes sur home-assistant/brands

Pour que l'icône apparaisse dans le picker "Ajouter une intégration" de HA,
il faut soumettre une PR sur le dépôt officiel `home-assistant/brands`.

## Structure à créer dans le fork de brands

```
custom_integrations/
└── bwt_aqa_perla_ble/
    ├── icon.png       ← 256×256 px  (copier depuis custom_components/)
    └── icon@2x.png    ← 512×512 px  (copier depuis custom_components/)
```

## Étapes

1. Fork https://github.com/home-assistant/brands
2. Créer le dossier `custom_integrations/bwt_aqa_perla_ble/`
3. Y copier `icon.png` et `icon@2x.png`
4. Ouvrir une Pull Request avec comme titre :
   `Add BWT AQA Perla BLE integration icons`
5. Dans la description de la PR, indiquer :
   - Lien vers le dépôt GitHub de l'intégration
   - Lien vers la page HACS (après publication)

## Notes

- Les icônes doivent être en PNG avec fond transparent
- 256×256 px minimum pour icon.png
- 512×512 px pour icon@2x.png
- La review par l'équipe HA prend généralement quelques jours
