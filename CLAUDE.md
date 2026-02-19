# Polymarket Compound Scalping Bot - 개발 메모

## 프로젝트 개요
Polymarket에서 크립토 가격 예측 시장(BTC, ETH, SOL 등)을 자동 스캘핑하는 봇.
Binance 실시간 가격 + 기술 지표(RSI, EMA, VWAP, BB, MACD, OFI) 기반 컨플루언스 스코어로 진입/청산.

## 핵심 아키텍처
- **전략 우선순위**: 5분봉 > 15분봉 > 전략A(가격 연동) > 전략B(뉴스, 비활성) > 전략C(오더북 불균형)
- **동시 포지션**: 최대 4개
- **루프 주기**: 10초
- **지갑 타입**: Proxy 지갑 (signature_type=1, Magic/이메일 로그인)

## API 중요 사항 (공식 문서 기반)
- **Gamma API** (`gamma-api.polymarket.com`): 마켓 조회
  - `tag=crypto`는 **사용하지 않음** (잘못된 결과 반환)
  - 볼륨 순 전체 스캔 + `detect_asset()` 클라이언트 필터링 사용
  - 응답 필드: camelCase (`conditionId`, `acceptingOrders`, `enableOrderBook`, `clobTokenIds`, `outcomePrices`, `volume24hr`, `negRisk`, `orderPriceMinTickSize`, `secondsDelay`)
  - `clobTokenIds`/`outcomes`/`outcomePrices`는 JSON 문자열 배열
- **CLOB API** (`clob.polymarket.com`): 주문/오더북/가격
  - `GET /price` → `{"price": 0.45}` (숫자)
  - `GET /midpoint` → `{"mid_price": "0.45"}` (문자열!)
  - `GET /book` → OrderBookSummary 객체, bids/asks는 OrderSummary 객체 (`.price`, `.size`는 문자열)
  - 주문 필수 파라미터: `tickSize` (문자열: "0.01"), `negRisk` (boolean)
- **Polygon RPC**: 기본 `polygon-rpc.com` + 4개 폴백 (publicnode, ankr, llama, drpc)

## 해결된 버그 이력
1. 토큰 ID 빈 문자열 → `clobTokenIds` + `outcomes` JSON 파싱 추가
2. camelCase vs snake_case 필드 불일치 → 양쪽 모두 시도하는 fallback
3. "Bitcoin" ↔ "BTC" 매칭 실패 → `ASSET_NAME_MAP` 추가
4. "NETHERLANDS" → "ETH" 오탐 → `ASSET_FALSE_POSITIVES` 추가
5. OrderSummary 객체 subscript 에러 → `getattr()` 사용
6. `get_price()` dict 반환 → `_extract_float()` 헬퍼
7. `get_midpoint()` 키 오류 → `"mid_price"` 사용
8. 5분봉/15분봉 전략 미작동 → 키워드 기반 → `detect_asset()` 기반으로 전환
9. `tag=crypto` 마켓 발견 실패 → 볼륨 순 전체 스캔으로 교체
10. Web3 RPC 연결 실패 → 다중 RPC 폴백
11. "DOGE" 정부부처 오탐 → bare "DOGE" 제거, "DOGECOIN"만 매칭

## 현재 설정값
- 컨플루언스 임계값: 진입=3, 높음=5, 최고=7
- 가격 변동 임계값: 0.3% (전략A)
- 오더북 불균형: 0.25 (전략C)
- 가격 범위: 0.05~0.95
- 최소 일일 볼륨: $30,000

## 자산 매칭 규칙
- `ASSET_NAME_MAP`: BITCOIN→BTC, ETHEREUM→ETH, SOLANA→SOL, DOGECOIN→DOGE
- 거짓양성 제외: NETHERLANDS, SOLVED, SOLUTION, ETHANOL, METHOD, GOVERNMENT EFFICIENCY, DEPARTMENT OF
- bare "DOGE"는 매칭하지 않음 (정부 DOGE와 혼동)
