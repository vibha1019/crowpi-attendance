#!/usr/bin/env bash
# Downloads the two face models camera mode needs into camera/models/, from
# the OpenCV Zoo (https://github.com/opencv/opencv_zoo):
#   face_detection_yunet_2023mar.onnx   finds faces            (about 230 KB)
#   face_recognition_sface_2021dec.onnx tells faces apart      (about 37 MB)
# Run it once from anywhere: ./get_models.sh
# Already-downloaded models are kept. Models are gitignored; never commit them.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p models

ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"
# file name, URL, smallest believable size in bytes (a real model, not a
# Git LFS pointer or an error page)
MODELS=(
  "face_detection_yunet_2023mar.onnx  $ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx  100000"
  "face_recognition_sface_2021dec.onnx  $ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx  10000000"
)

file_size() { wc -c < "$1" | tr -d ' '; }

for entry in "${MODELS[@]}"; do
  read -r name url min_bytes <<< "$entry"
  target="models/$name"
  if [ -f "$target" ] && [ "$(file_size "$target")" -ge "$min_bytes" ]; then
    echo "Already have $target ($(file_size "$target") bytes)"
    continue
  fi
  echo "Downloading $name ..."
  # -L follows GitHub's redirect to the real (Git LFS) file.
  curl -fL --retry 3 --progress-bar -o "$target.part" "$url"
  size=$(file_size "$target.part")
  if [ "$size" -lt "$min_bytes" ]; then
    rm -f "$target.part"
    echo "ERROR: $name is only $size bytes, so it is not the real model." >&2
    echo "Open $url in a browser, download it, and save it as camera/$target" >&2
    exit 1
  fi
  mv "$target.part" "$target"
  echo "Saved $target ($size bytes)"
done

echo
echo "Model sizes:"
ls -lh models/*.onnx
