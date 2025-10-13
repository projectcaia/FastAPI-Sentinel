# 🔧 DB증권 API 문제 해결 가이드

## 🚨 현재 문제: 403 Forbidden

**증상:**
```
ERROR - DB증권 토큰 발급 실패: 403
```

---

## 📋 체크리스트

### 1. 종목 코드 확인

**현재 설정 확인:**
```bash
# Railway → sentinel-worker → Variables
DB_FUTURES_CODE=?????
```

**2025년 12월물 코드:**
- 네이버/다음: `K200 F 202512`
- DB증권 API: `101WC000` (12월물)

**Railway 환경 변수 설정:**
```bash
DB_FUTURES_CODE=101WC000
```

**⚠️ 주의:** 
- 분기마다 롤오버 필요 (3/6/9/12월)
- 2025년 12월 만료 → 2026년 3월물로 변경

---

### 2. API 키 확인

**Railway 환경 변수:**
```bash
DB_APP_KEY=your_app_key_here
DB_APP_SECRET=your_app_secret_here
```

**확인 방법:**
1. DB증권 API 포털 접속
2. My API → 키 관리
3. 만료일 확인
4. 필요시 재발급

---

### 3. API 엔드포인트 확인

**기본 URL:**
```bash
DB_API_BASE=https://openapi.dbsec.co.kr:8443
```

**변경 확인 필요:**
- DB증권이 API URL을 변경했을 수 있음
- 최신 문서 확인: https://openapi.dbsec.co.kr

---

## 🔍 디버깅 방법

### Railway 로그 확인

```bash
railway logs --service sentinel-worker | grep "K200"
```

**정상 로그:**
```
📊 K200 선물 체크 시작...
✓ K200 선물: 현재=498.70, 변화=-1.80%
```

**에러 로그:**
```
ERROR - DB증권 토큰 발급 실패: 403 - 종목코드:101WC000 - {"error": "..."}
ERROR - K200 선물 가격 조회 실패: 403 - 종목코드:101WC000 - {"error": "..."}
```

### 수동 API 테스트

**1. 토큰 발급 테스트:**
```bash
curl -X POST "https://openapi.dbsec.co.kr:8443/oauth2/token" \
  -H "Accept: application/json" \
  --data-urlencode "appkey=YOUR_APP_KEY" \
  --data-urlencode "appsecretkey=YOUR_APP_SECRET" \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "scope=oob"
```

**정상 응답:**
```json
{
  "access_token": "eyJhbGciOiJI...",
  "expires_in": 3600,
  "token_type": "Bearer"
}
```

**2. 선물 가격 조회 테스트:**
```bash
curl -X POST "https://openapi.dbsec.co.kr:8443/dfutureoption/quotations/v1/inquire-price" \
  -H "Content-Type: application/json" \
  -H "authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "appkey: YOUR_APP_KEY" \
  -H "appsecret: YOUR_APP_SECRET" \
  -H "custtype: P" \
  -H "tr_id: HHDFS76240000" \
  -d '{
    "fid_cond_mrkt_div_code": "F",
    "fid_input_iscd": "101WC000",
    "fid_input_iscd_cd": "1"
  }'
```

---

## 🛠️ 해결 방법

### 방법 1: 종목 코드 수정

```bash
# Railway → sentinel-worker → Variables
DB_FUTURES_CODE=101WC000  # 2025년 12월물
```

**재시작:**
```bash
railway restart --service sentinel-worker
```

### 방법 2: API 키 재발급

1. DB증권 API 포털 로그인
2. My API → 키 재발급
3. Railway 환경 변수 업데이트
4. 서비스 재시작

### 방법 3: 임시 비활성화 (테스트용)

```bash
# K200 선물 감시 비활성화
DBSEC_ENABLE=false
```

**재활성화:**
```bash
DBSEC_ENABLE=true
```

---

## 📊 종목 코드 참조표

| 만기 | 네이버/다음 | DB증권 API | 비고 |
|------|-----------|-----------|------|
| 2025년 12월 | K200 F 202512 | `101WC000` | 현재 |
| 2026년 3월 | K200 F 202603 | `101RC000` | 다음 |
| 2026년 6월 | K200 F 202606 | `101SC000` | 차다음 |

**패턴:**
- 101 = KOSPI 200 선물
- R/W/S = 분기 (3/6/9/12월)
- C = 2026년 (B=2025년)
- 000 = 일반

---

## 🔄 정기 점검 (분기별)

### 3/6/9/12월 만기일 전

1. **다음 월물 코드 확인**
   ```
   네이버 증권 → KOSPI 200 선물 → 종목 코드 확인
   ```

2. **Railway 환경 변수 업데이트**
   ```bash
   DB_FUTURES_CODE=새로운_코드
   ```

3. **서비스 재시작**
   ```bash
   railway restart --service sentinel-worker
   ```

4. **로그 확인**
   ```bash
   railway logs --service sentinel-worker | grep "K200"
   ```

---

## 🎯 예상 에러 및 해결

### 에러 1: 403 Forbidden

**원인:**
- API 키 만료
- 종목 코드 오류
- IP 차단

**해결:**
1. API 키 재발급
2. 종목 코드 확인
3. DB증권 고객센터 문의

### 에러 2: 404 Not Found

**원인:**
- 잘못된 종목 코드
- 만기된 종목

**해결:**
1. 네이버 증권에서 최근월물 코드 확인
2. `DB_FUTURES_CODE` 업데이트

### 에러 3: 500 Internal Server Error

**원인:**
- DB증권 API 서버 문제
- 거래 시간 외

**해결:**
1. 거래 시간 확인 (09:00-15:30, 18:00-05:00)
2. 주말/공휴일 아닌지 확인
3. 잠시 후 재시도

---

## 📞 지원

### DB증권 API 고객센터
- 전화: 1588-xxxx
- 이메일: api@dbsec.com
- 문서: https://openapi.dbsec.co.kr/docs

### Railway 로그 확인
```bash
# 실시간 로그
railway logs --service sentinel-worker --tail

# K200 관련 로그만
railway logs --service sentinel-worker | grep "K200"

# 에러 로그만
railway logs --service sentinel-worker | grep "ERROR"
```

---

## ✅ 체크리스트 (순서대로 확인)

- [ ] Railway 환경 변수에 `DB_APP_KEY` 존재
- [ ] Railway 환경 변수에 `DB_APP_SECRET` 존재
- [ ] Railway 환경 변수에 `DB_FUTURES_CODE=101WC000` 설정
- [ ] DB증권 API 키 만료일 확인
- [ ] 거래 시간 확인 (평일 09:00-15:30, 18:00-05:00)
- [ ] 주말/공휴일 아닌지 확인
- [ ] Railway 서비스 재시작
- [ ] 로그에서 "✓ K200 선물" 메시지 확인

---

**작성일:** 2025-10-13  
**버전:** 1.0  
**마지막 업데이트:** 2025년 12월물 기준 (101WC000)
