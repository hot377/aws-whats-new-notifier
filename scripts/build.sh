#!/usr/bin/env bash
# Lambda デプロイパッケージ（dist/lambda.zip）をビルドする。
# 依存ライブラリ（feedparser / sgmllib3k / beautifulsoup4 / soupsieve）はすべて
# 純 Python でコンパイル済みバイナリを含まないため、ビルドマシンの
# OS / アーキテクチャに依存しない。また feedparser が依存する sgmllib3k は
# wheel が公開されていない（sdist のみ）ため、
# pip の --platform / --only-binary による wheel 限定指定は使えない。
# コンパイルを要する依存を将来追加する場合はこの前提が崩れるので、
# その際は Lambda の実行環境（arm64 / Python 3.13）向けの wheel を --platform 指定で取得し、
# sgmllib3k だけ個別に扱うこと。
set -euo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="${root_dir}/build"
dist_dir="${root_dir}/dist"

rm -rf "${build_dir}" "${dist_dir}"
mkdir -p "${build_dir}" "${dist_dir}"

python3 -m pip install \
  --quiet \
  --no-compile \
  --target "${build_dir}" \
  -r "${root_dir}/lambda/requirements.txt"

cp \
  "${root_dir}/lambda/crawler.py" \
  "${root_dir}/lambda/notifier.py" \
  "${root_dir}/lambda/alarm_notifier.py" \
  "${root_dir}/lambda/prompt.md" \
  "${root_dir}/lambda/alarm_prompt.md" \
  "${build_dir}/"

# __pycache__ はビルドマシンの Python バージョンの .pyc なので含めない
(cd "${build_dir}" && zip -qr "${dist_dir}/lambda.zip" . -x '*__pycache__*')

echo "built: ${dist_dir}/lambda.zip"
