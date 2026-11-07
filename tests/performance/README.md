# Prueba de carga de la inferencia

Mide cuántos usuarios concurrentes aguanta la infraestructura actual (una EC2 `t3.small`) sin pasar el SLO.
Con ese número se decide cuánto escalar según los usuarios esperados.

| Archivo | Qué responde |
|---|---|
| `locustfile.py` | Línea base: ¿cuánto tarda un clic sin carga? |
| `stress.py` | Capacidad: ¿hasta cuántos usuarios se cumple el SLO? |
| `slo.py` | Umbrales y reportes |

No hay escenario nominal: los requisitos no definen cuántos usuarios esperar (decisión de Kevin, 2026-11-07).

## SLO

| Métrica | Objetivo |
|---|---|
| p95 del servidor | < 1 s por clic (`new_arch.md` §5) |
| Respuestas correctas | ≥ 99 % |

Se juzga con el tiempo **dentro del servicio**, que la API devuelve en el header `Server-Timing`. Desde la
laptop, la red hasta `us-east-1` suma cientos de milisegundos que no son de la infraestructura. El reporte
muestra también el tiempo del cliente y el de `/health`, que es casi pura red.

## Usuario simulado

Un visitante que hace clic en un aviso cada ~2 s en promedio, con espera exponencial (llegan en rachas).
Los clics usan `spot_id` reales del GeoIndex de la corrida en producción. El 10 % pide un `spot_id`
inexistente: la API responde 404 y eso cuenta como correcto. Si cambia el `run_id` a mitad de la prueba,
el clic cuenta como fallo.

## Correr

Desde la raíz, con la EC2 encendida y tu IP autorizada:

```bash
export HOST=http://$(AWS_PROFILE=smartestate terraform -chdir=infra output -raw inference_public_ip):8000
export RUNS_ROOT=s3://smartestate-lake-<account_id>/runs
mkdir -p tests/performance/results

# Línea base: 1 usuario, 3 minutos
./ss/bin/locust -f tests/performance/locustfile.py --host "$HOST" --headless \
  -u 1 -r 1 --run-time 3m --csv tests/performance/results/baseline

# Estrés: 5 → 10 → 20 → 40 → 80 → 160 usuarios, 3 minutos cada escalón (18 min)
./ss/bin/locust -f tests/performance/stress.py --host "$HOST" --headless \
  --csv tests/performance/results/stress
```

Se ajusta sin tocar código: `STRESS_STEPS=20,40,80,160,320 STRESS_STEP_MINUTES=2`. Si ningún escalón
falla, el reporte lo dice: hay que subir los escalones.

Locust corre fuera de la EC2: si corriera dentro, competiría por las mismas 2 vCPU que mide.
Mientras corre, conviene mirar la memoria y la CPU del contenedor:

```bash
aws ssm start-session --profile smartestate --target <instance_id>
docker stats
```

## Leer el resultado

El estrés imprime una fila por escalón y la **capacidad**: el último escalón que cumple el SLO antes del
primero que falla. Cada escalón descarta sus primeros 20 s, para que la subida de usuarios no cuente.

| Si sube... | Significa |
|---|---|
| El p95 del servidor | Falta CPU o hay cola en la EC2, o DynamoDB tarda más |
| Solo el p95 del cliente | Es la red o el límite de conexiones, no el servicio |
| La red (`/health`) | El camino hasta la EC2, no el servicio |
