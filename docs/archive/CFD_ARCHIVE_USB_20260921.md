# CFD 원본 USB 이전 기록 (2026-09-21)

대용량 OpenFOAM 원본 두 건을 내부 `build/`에서 USB로 이전했다.

## 보관 위치

USB 볼륨: `승민`

폴더: `YU_fluid_CFD_원본_20260921`

- `ppt_bernardi_800k.tar`
  - TAR 크기: 43,223,509,504 bytes
  - 원본 파일: 31,114개
  - 비교한 원본 데이터: 43,184,526,548 bytes
- `energy_coupling_adaptive_cold_pilot.tar`
  - TAR 크기: 30,214,362,112 bytes
  - 원본 파일: 28,155개
  - 비교한 원본 데이터: 30,163,718,316 bytes

두 TAR은 생성 후 모든 일반 파일을 내부 원본과 바이트 단위로 전수 비교했으며 모두 일치했다. macOS가 TAR에 추가한 `._` AppleDouble 메타데이터 항목은 CFD 데이터 비교에서 제외했다.

## 복원 방법

USB가 연결된 상태에서 프로젝트 루트에서 다음과 같이 복원한다.

```bash
tar -xf "/Volumes/승민/YU_fluid_CFD_원본_20260921/ppt_bernardi_800k.tar" -C build
tar -xf "/Volumes/승민/YU_fluid_CFD_원본_20260921/energy_coupling_adaptive_cold_pilot.tar" -C build
```

OpenFOAM 계산을 다시 수행하기 전에는 exFAT USB에서 직접 실행하지 말고 APFS 내부 저장장치로 복원한다. 현재 결과 뷰어와 배포 ZIP은 이 원본과 별도로 `build/`에 유지된다.
