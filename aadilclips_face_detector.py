import json
import sys


def detect(video_path, start, duration):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("Face Follow requires opencv-python-headless to be installed") from exc
    cv2.setNumThreads(1)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if detector.empty():
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    points = []
    last = None
    saw_face = False
    try:
        steps = min(61, max(1, int(duration) + 1))
        for second in range(steps):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + second) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            original_height, original_width = frame.shape[:2]
            scale = 320 / max(original_height, original_width)
            small = cv2.resize(frame, (max(1, round(original_width * scale)), max(1, round(original_height * scale))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=4, minSize=(22, 22))
            if len(faces):
                saw_face = True
                if last is None:
                    best = max(faces, key=lambda f: f[2] * f[3])
                else:
                    best = max(faces, key=lambda f: (f[2] * f[3]) / (1 + 9 * ((f[0] + f[2] / 2) / gray.shape[1] - last[0]) ** 2 + 9 * ((f[1] + f[3] / 2) / gray.shape[0] - last[1]) ** 2))
                x, y, w, h = map(float, best)
                cx = (x + w / 2) / gray.shape[1]
                cy = (y + h * 0.42) / gray.shape[0]
                if last is not None:
                    cx = last[0] * 0.55 + cx * 0.45
                    cy = last[1] * 0.55 + cy * 0.45
                last = (cx, cy)
            elif last is not None:
                cx, cy = last
            else:
                cx, cy = 0.5, 0.5
            points.append({"t": float(second), "x": round(max(0.0, min(1.0, cx)), 5), "y": round(max(0.0, min(1.0, cy)), 5)})
    finally:
        cap.release()
    return points if saw_face else []


if __name__ == "__main__":
    try:
        print(json.dumps(detect(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
