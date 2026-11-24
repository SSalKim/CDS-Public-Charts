# R2 manifest 발행 (1단계: 이중 쓰기)

공개 manifest를 Git/raw 대신 Cloudflare R2에서 배포하기 위한 1단계다.
이 단계에서 이미지는 계속 Google Drive에 있고, manifest만 R2에도 같이 쓴다.
Git manifest 커밋은 그대로 유지하며, 뷰어 전환과 1일 비교가 끝난 뒤에 끈다.
1일이면 모든 cycle 종류(00/06/12/18 UTC, 12시간 간격 모델, UKMO 게이트)와 만료 cycle 정리가 한 번 이상 지나간다.

## 객체 구조

```
manifest/index.json                          Cache-Control: public, max-age=15
manifest/runs/{model}/{cycle}.{rev}.json     Cache-Control: public, max-age=31536000, immutable
```

`index.json`:

```json
{
  "schema": 2,
  "pipeline_version": 7,
  "generated_at": "2026-11-19T06:00:00+00:00",
  "images": "drive",
  "models": {"gfs": {"title": "NCEP GFS (C768 L127)", "published": true, "retention_cycles": 9}},
  "runs": {"gfs/2026111900": {"rev": "3f9a1c2e0b7d4a61", "count": 212}}
}
```

run 조각:

```json
{
  "schema": 2, "model": "gfs", "cycle": "2026111900", "images": "drive",
  "leads": [0, 3, 6, 240],
  "charts": [[0, "asia", "gph500", "<Drive file_id>"],
             [0, "hkor", "acptot", "<file_id>", {"kma_confirmed_at": "...", "kma_reference_hour": 0}]]
}
```

- `charts` 항목은 `[hour, domain, product, ref]`이고, UKMO 확인 정보만 다섯 번째 원소(객체)로 붙는다.
- `images: "drive"`이면 `ref`는 Drive file_id다. URL은
  `https://drive.usercontent.google.com/download?id={ref}&export=view&authuser=0`로 만든다.
  2단계에서 이미지를 R2로 옮기면 `images: "r2"`가 되고 `ref`는 이미지 digest가 된다.
- `leads`는 생산 쪽 `policy.hours(model, cycle)`이다. 뷰어는 이것을 예보시간 목록의 원본으로 쓴다.
- `rev`는 조각 내용의 SHA-256 앞 16자리다. 같은 내용이면 같은 키가 되고, 이미 올라간 키는 다시 쓰지 않는다.
- `generated_at`은 index를 쓴 시각이다. run이 도는 동안에는 내용이 같아도 5분마다 다시 쓰지만, 새 자료가 없는 시간대에는
  run이 끝날 때만 써서 수십 분 묵을 수 있다. 그래서 뷰어는 기기 시계가 이보다 늦거나 2시간 넘게 앞설 때만 이 값으로 보정한다.

## 쓰기 순서와 비용

1. 바뀐 run 조각만 새 rev 키로 PUT한다(기존 키는 덮어쓰지 않는다).
2. 마지막에 `index.json`을 PUT한다. 30초 안의 변경은 묶어서 쓰고, run이 끝날 때는 바로 쓴다.
3. 생산 루프는 ListObjects를 호출하지 않는다. 시작할 때 `index.json`을 한 번 GET하지만, 거기 적힌 조각이 아직 있다고
   믿지 않는다. 각 프로세스는 첫 게시 때 모든 조각을 다시 PUT하고, 그 뒤로는 바뀐 조각만 올린다.
4. 오래된 조각은 지우지 않고 Lifecycle 규칙(2일)에 맡긴다. 그래서 15초 캐시 안의 옛 index가 가리키는 조각도 계속 읽을 수 있다.
   내용이 바뀌지 않는 run(예: CMC는 12시간 간격 5 cycle, 60시간 보존)은 rev가 그대로라서, 다시 올리지 않으면 2일 뒤
   조각만 지워지고 index는 계속 그 조각을 가리킨다. 2026-11-24에 `cmc/2026112200` 조각이 이렇게 사라져 뷰어가 자체생산
   모델 전체를 표시하지 못했다. 실행마다 모든 조각을 다시 올리므로 어떤 조각도 2일을 넘기지 않는다(실행당 PUT 약 55건,
   하루 약 3,300건 추가로 월 무료 한도 안).

예상 Class A(쓰기)는 index 최대 하루 약 2,900건, 조각 수천 건이다. 월 20만 건 안팎으로, 무료 한도(100만 건) 안에 든다.
로그의 `r2_index_published` 이벤트에 프로세스별 누적 `r2_writes`·`r2_reads`가 남는다.

## 모드

저장소 변수 `PUBLIC_CHARTS_R2_MODE`:

| 값 | 동작 |
| --- | --- |
| 비어 있음 | secret 네 개가 모두 있으면 `shadow`, 아니면 `off` |
| `off` | R2를 쓰지 않음 |
| `shadow` | R2에 쓰되, 실패는 `r2_publish_failed`로만 남기고 생산·정리는 그대로 진행 |
| `required` | 마지막 발행 단계에서 R2가 실패하면 Drive 정리(만료 이미지 삭제)를 막음 |

뷰어가 R2를 읽기 시작하면 `required`로 바꾼다. 그래야 R2 index가 가리키는 이미지가 지워지지 않는다.
실행 중의 R2 실패는 어느 모드에서도 생산을 멈추지 않는다.

R2 자격증명은 GitHub-hosted coordinator에만 있다. 노트북 helper는 `PUBLIC_CHARTS_R2_*` 환경변수를 받으면 시작하지 않는다.

## 이미지 (2단계: 이중 쓰기)

저장소 변수 `PUBLIC_CHARTS_R2_IMAGES`:

| 값 | 동작 |
| --- | --- |
| 비어 있음, `off` | 이미지는 Drive에만 올린다 |
| `dual` | Drive 업로드가 끝난 같은 업로드 스레드에서 R2에도 PUT한다(R2 모드가 `off`가 아닐 때만) |

- 키: `charts/{model}/{cycle}/{model}_{domain}_{product}_s{HHH}_{cycle}_{sha256 앞 16자}.webp`,
  `Cache-Control: public, max-age=31536000, immutable`, `Content-Type: image/webp`.
- PUT이 성공한 이미지만 행에 `r2`(객체 이름)가 붙는다. Git manifest 행과 run 조각의 다섯 번째 원소(`{"r2": ...}`)에 들어간다.
  alias 행(예: `surfce` → `acptot`)은 원본 이미지의 이름을 그대로 쓴다. 이름의 model·cycle은 행과 같아야 한다.
- PUT이 실패하면 `r2_image_failed`를 남기고 그 행은 Drive 주소만 가진다. 생산은 멈추지 않는다.
- 뷰어는 R2 조각에서 `r2`가 있으면 커스텀 도메인 주소를 쓰고, 로드에 실패하면 같은 이미지의 Drive 주소로 넘어간다.
  R2 index를 못 읽어 Git manifest로 내려간 경우에는 Drive 주소만 쓴다.
- 런 끝 요약 줄에 `r2_image_writes`, `r2_image_failures`, `r2_image_bytes`가 남는다.
- R2 이미지 삭제는 Lifecycle 규칙(`charts/` 4일)에 맡긴다. 보존 기간(최대 9 cycle, 54시간)보다 길다.

### `only`: R2 단일 이미지 (2026-11-21~)

- `PUBLIC_CHARTS_R2_IMAGES=only`이면 업로드 스레드는 R2에만 PUT한다. R2 PUT이 실패한 이미지만 Drive로 올린다(런 요약의 `drive_fallbacks`).
- R2 전용 행: `storage_account="r2"`, `file_id="r2_<객체 이름에서 .webp를 뺀 것>"`, `url`은 커스텀 도메인 주소, `r2`는 객체 이름.
  `split_manifest`는 세 값이 서로 맞는지 확인한다.
- 시작 시 Drive inventory에 없는 행은 버리지만, R2 전용 행은 남긴다. 보존·용량 계산에서 R2는 한 계정(`r2`, 상한 60 GiB)으로 다룬다.
- Drive 정리 계획(`delete_ids`)에는 R2 전용 행이 들어가지 않는다. R2 객체는 Lifecycle 규칙(`charts/` 4일)이 지운다.
- 뷰어는 `r2_` id 항목을 R2 주소로만 읽는다(Drive 대체 없음).
- 되돌리기: 변수를 `dual`로 바꾸면 새 이미지는 다시 Drive와 R2에 함께 올라간다. 이미 R2에만 있는 행은 만료될 때까지 그대로 유지된다.
- `state.json`·`manifest.json` 같은 비공개 파일은 아직 Drive에 있다.

## Cloudflare 설정 (사용자가 대시보드에서 할 일)

1. **버킷**: R2 → Create bucket. 이름 예: `cds-public-charts`. Location은 Automatic, 등급은 Standard.
2. **커스텀 도메인**: 버킷 → Settings → Custom Domains → Connect Domain(예: `charts.<내 도메인>`).
   `r2.dev` 공개 주소는 켜지 않는다(캐시를 거치지 않아 모든 읽기가 Class B로 청구된다).
3. **Cache Rule**: 해당 존 → Caching → Cache Rules → 새 규칙
   - 조건: Hostname이 커스텀 도메인과 같고, URI Path가 `/manifest/`로 시작
   - 동작: Eligible for cache, Edge TTL은 "Use cache-control header if present"
   - Cloudflare는 `.json`을 기본으로 캐시하지 않으므로 이 규칙이 꼭 필요하다.
4. **CORS**: 버킷 → Settings → CORS Policy

   ```json
   [{"AllowedOrigins": ["https://ssalkim.github.io"], "AllowedMethods": ["GET", "HEAD"],
     "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 86400}]
   ```

   로컬에서 뷰어를 시험하려면 `http://localhost:8000` 같은 주소를 추가한다.
5. **Lifecycle**: 버킷 → Settings → Object lifecycle rules
   - `manifest/runs/`: 업로드 후 2일이 지나면 삭제
   - (2단계용) `charts/`: 업로드 후 4일이 지나면 삭제
   - `manifest/index.json`에는 규칙을 걸지 않는다.
6. **API 토큰**: R2 → Manage R2 API Tokens → Create API token
   - 권한: Object Read & Write, **이 버킷 하나만** 지정
   - 발급되는 Access Key ID, Secret Access Key를 바로 아래 secret에 넣는다.
7. **GitHub secret** (`SSalKim/CDS-Public-Charts` → Settings → Secrets and variables → Actions)
   - `PUBLIC_CHARTS_R2_ACCOUNT_ID`: Cloudflare 계정 ID(32자리 16진수)
   - `PUBLIC_CHARTS_R2_ACCESS_KEY_ID`, `PUBLIC_CHARTS_R2_SECRET_ACCESS_KEY`
   - `PUBLIC_CHARTS_R2_BUCKET`: 버킷 이름
8. **결제 알림**: Manage Account → Billing → Notifications에서 사용량 알림을 켠다.

secret을 넣으면 다음 coordinator run부터 `shadow`로 이중 쓰기를 시작한다.
로그에서 `{"event": "r2_publication", "mode": "shadow"}`와 `r2_index_published`를 확인한다.

## 확인 방법

```bash
curl -sI https://charts.<내 도메인>/manifest/index.json   # cf-cache-status, cache-control 확인
curl -s  https://charts.<내 도메인>/manifest/index.json | python -m json.tool | head
```

같은 시점의 Git manifest와 R2 조각을 비교하는 스크립트는 뷰어 전환 단계에서 추가한다.
비교 기간을 1일로 줄이는 대신, 뷰어는 R2를 읽다가 실패하면 Git manifest로 자동으로 돌아가게 만든다.
