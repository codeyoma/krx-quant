# krx-quant

KRX 종목의 4분 뒤 가격 움직임을 여러 퀀트 알고리즘이 예측하고, 예측을 자동 채점해 통계로 보여주는 개인용 엔진이다. 결과는 fork한 Fincept Terminal([codeyoma/FinceptTerminal](https://github.com/codeyoma/FinceptTerminal), `custom` 브랜치)의 화면에서 본다.

주문 기능은 없다. 신호만 낸다.

## 받기

```bash
git clone --recurse-submodules https://github.com/codeyoma/krx-quant.git
```

`fincept/`는 공개 fork `codeyoma/FinceptTerminal`의 `custom` 브랜치를 가리키는 submodule이다. 작업 순서는 [AGENTS.md](AGENTS.md)의 Submodule workflow를 따른다.

## 문서

- [docs/design.md](docs/design.md): 설계 (목표, 구조, 데이터, 알고리즘, 학습, 인프라, 개발 단계)
- [docs/paid-data.md](docs/paid-data.md): 유료 데이터 조사

## 진행 상황

| 단계 | 내용 | 상태 |
|---|---|---|
| P0 | 저장소 준비, 설계 문서 | 완료 |
| P1 | 수집기 | 다음 |
| P2 | 예측·채점 핵심 | |
| P3 | Fincept 화면, 빌드 | |
| P4 | 밤 학습 (EC2 Spot) | |
| P5 | 호가 알고리즘, 재생 테스트 | |

## 비밀값

API 키와 토큰은 서버의 `.env`에만 둔다. 저장소에 커밋하지 않는다.
