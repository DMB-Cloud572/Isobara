# Isobara

Predicción meteorológica para España: compara 8 modelos, calcula un consenso con su fiabilidad y mide cuánto acierta cada uno.

## Cambiar municipios

Edita `municipios.json` desde GitHub. Añade o quita nombres y guarda. Unos minutos después, la app los muestra.

Si el nombre lleva a un lugar equivocado, indica las coordenadas:

```json
["Pinto", {"nombre": "Otro lugar", "sub": "Provincia", "lat": 42.0, "lon": -5.0}]
```

Quitar un municipio no borra su historial.

## Funcionamiento

- `scripts/recoger.py` se ejecuta 4 veces al día en GitHub Actions.
- Guarda una foto diaria de lo que predice cada modelo (`data/predicciones/`).
- Guarda las observaciones de la estación de AEMET más cercana con datos (`data/observaciones/`, `data/estaciones.json`).
- Calcula el error de cada modelo y del consenso (`data/aciertos.json`).

La clave de AEMET se guarda como secreto `AEMET_API_KEY` en Settings → Secrets and variables → Actions. Sin ella, se siguen guardando las predicciones.

## Datos

Predicciones: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0). Observaciones: Agencia Estatal de Meteorología (AEMET).
