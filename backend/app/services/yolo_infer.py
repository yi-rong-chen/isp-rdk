from __future__ import annotations

from time import time
import logging

import cv2
import numpy as np
from scipy.special import softmax
from hobot_dnn import pyeasy_dnn as dnn

logger = logging.getLogger("RDK_YOLO")

RDK_COLORS = [
    (56, 56, 255),
    (151, 157, 255),
    (31, 112, 255),
    (29, 178, 255),
    (49, 210, 207),
    (10, 249, 72),
    (23, 204, 146),
    (134, 219, 61),
    (52, 147, 26),
    (187, 212, 0),
    (168, 153, 44),
    (255, 194, 0),
    (147, 69, 52),
    (255, 115, 100),
    (236, 24, 0),
    (255, 56, 132),
    (133, 0, 82),
    (255, 56, 203),
    (200, 149, 255),
    (199, 55, 255),
]


class YOLODetector:
    def __init__(self, model_file: str, classes_num: int, reg: int, conf: float, iou: float) -> None:
        begin_time = time()
        self.quantize_model = dnn.load(model_file)
        logger.info("model loaded in %.2f ms", 1000 * (time() - begin_time))

        self.model_input_height, self.model_input_width = self.quantize_model[0].inputs[0].properties.shape[2:4]
        self.classes_num = classes_num
        self.reg = reg
        self.conf = conf
        self.iou = iou
        self.conf_inverse = -np.log(1 / conf - 1)

        self.pad_left = 0
        self.pad_top = 0
        self.scale = 1.0
        self.img_w = 0
        self.img_h = 0

        self.weights_static = np.arange(self.reg, dtype=np.float32)[np.newaxis, np.newaxis, :]
        self.branches = self._build_branches()

    def set_identity_geometry(self, width: int, height: int) -> None:
        # Input frame is already model-sized NV12, no letterbox transform.
        self.img_w = width
        self.img_h = height
        self.pad_left = 0
        self.pad_top = 0
        self.scale = 1.0

    def _build_branches(self) -> dict[int, dict[str, np.ndarray | int]]:
        branches: dict[int, dict[str, np.ndarray | int]] = {}
        for output in self.quantize_model[0].outputs:
            shape = output.properties.shape
            if len(shape) != 4:
                continue
            h, w, c = shape[1:]
            if h != w:
                continue

            if c == self.reg * 4:
                stride = self.model_input_width // h
                anchor = np.stack(
                    [
                        np.tile(np.linspace(0.5, h - 0.5, h), reps=h),
                        np.repeat(np.arange(0.5, h + 0.5, 1), h),
                    ],
                    axis=0,
                ).transpose(1, 0)
                branches[h] = {"stride": stride, "anchor": anchor}

        if not branches:
            raise ValueError("No bbox branches found from model outputs")

        logger.info("detected branches: %s", sorted(branches.keys(), reverse=True))
        return branches

    def _resizer(self, img: np.ndarray) -> np.ndarray:
        img_h, img_w = img.shape[:2]
        self.img_h, self.img_w = img_h, img_w

        scale = min(self.model_input_width / img_w, self.model_input_height / img_h)
        new_w = int(round(img_w * scale))
        new_h = int(round(img_h * scale))

        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        pad_w = self.model_input_width - new_w
        pad_h = self.model_input_height - new_h
        self.pad_left = pad_w // 2
        self.pad_top = pad_h // 2
        self.scale = scale

        return cv2.copyMakeBorder(
            resized,
            self.pad_top,
            pad_h - self.pad_top,
            self.pad_left,
            pad_w - self.pad_left,
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )

    def bgr_to_nv12(self, bgr_img: np.ndarray) -> np.ndarray:
        bgr_img = self._resizer(bgr_img)
        height, width = bgr_img.shape[:2]
        area = height * width
        yuv420p = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2YUV_I420).reshape((area * 3 // 2,))

        y = yuv420p[:area]
        uv_planar = yuv420p[area:].reshape((2, area // 4))
        uv_packed = uv_planar.transpose((1, 0)).reshape((area // 2,))

        nv12 = np.zeros_like(yuv420p)
        nv12[:area] = y
        nv12[area:] = uv_packed
        return nv12

    def forward(self, input_tensor: np.ndarray) -> list[np.ndarray]:
        outputs = self.quantize_model[0].forward(input_tensor)
        return [tensor.buffer for tensor in outputs]

    def _split_outputs(self, outputs: list[np.ndarray]) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
        bbox_outputs: dict[int, np.ndarray] = {}
        cls_outputs: dict[int, np.ndarray] = {}

        for output in outputs:
            if output.ndim != 4:
                continue
            h, w, c = output.shape[1:]
            if h != w:
                continue
            if c == self.reg * 4:
                bbox_outputs[h] = output.reshape(-1, self.reg * 4)
            elif c == self.classes_num:
                cls_outputs[h] = output.reshape(-1, self.classes_num)

        missing = [h for h in self.branches if h not in bbox_outputs or h not in cls_outputs]
        if missing:
            raise ValueError(f"Missing output branches: {missing}")

        return bbox_outputs, cls_outputs

    def post_process(self, outputs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        begin_time = time()
        bbox_outputs, cls_outputs = self._split_outputs(outputs)

        all_boxes = []
        all_scores = []
        all_ids = []

        for h, meta in self.branches.items():
            bboxes = bbox_outputs[h]
            clses = cls_outputs[h]

            max_scores = np.max(clses, axis=1)
            valid_indices = np.flatnonzero(max_scores >= self.conf_inverse)
            if len(valid_indices) == 0:
                continue

            ids = np.argmax(clses[valid_indices, :], axis=1)
            scores = 1 / (1 + np.exp(-max_scores[valid_indices]))
            bboxes_valid = bboxes[valid_indices, :]

            ltrb = np.sum(
                softmax(bboxes_valid.reshape(-1, 4, self.reg), axis=2) * self.weights_static,
                axis=2,
            )
            anchor = meta["anchor"][valid_indices, :]  # type: ignore[index]
            stride = int(meta["stride"])  # type: ignore[arg-type]

            x1y1 = anchor - ltrb[:, 0:2]
            x2y2 = anchor + ltrb[:, 2:4]
            dbboxes = np.hstack([x1y1, x2y2]) * stride

            all_boxes.append(dbboxes)
            all_scores.append(scores)
            all_ids.append(ids)

        if not all_boxes:
            empty_ids = np.empty((0,), dtype=np.int32)
            empty_scores = np.empty((0,), dtype=np.float32)
            empty_bboxes = np.empty((0, 4), dtype=np.int32)
            return empty_ids, empty_scores, empty_bboxes

        dbboxes = np.concatenate(all_boxes, axis=0)
        scores = np.concatenate(all_scores, axis=0)
        ids = np.concatenate(all_ids, axis=0)

        indices = cv2.dnn.NMSBoxes(dbboxes, scores, self.conf, self.iou)
        if indices is None or len(indices) == 0:
            empty_ids = np.empty((0,), dtype=np.int32)
            empty_scores = np.empty((0,), dtype=np.float32)
            empty_bboxes = np.empty((0, 4), dtype=np.int32)
            return empty_ids, empty_scores, empty_bboxes

        indices = np.array(indices).reshape(-1)
        bboxes = dbboxes[indices].copy()

        bboxes[:, [0, 2]] -= self.pad_left
        bboxes[:, [1, 3]] -= self.pad_top
        bboxes /= self.scale
        bboxes[:, [0, 2]] = np.clip(bboxes[:, [0, 2]], 0, self.img_w - 1)
        bboxes[:, [1, 3]] = np.clip(bboxes[:, [1, 3]], 0, self.img_h - 1)
        bboxes = bboxes.astype(np.int32)

        logger.debug("post process %.2f ms", 1000 * (time() - begin_time))
        return ids[indices], scores[indices], bboxes

    def infer(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        input_tensor = self.bgr_to_nv12(frame)
        outputs = self.forward(input_tensor)
        return self.post_process(outputs)

    def infer_nv12(self, nv12: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if width != self.model_input_width or height != self.model_input_height:
            raise ValueError(
                f"infer_nv12 expects {self.model_input_width}x{self.model_input_height}, got {width}x{height}"
            )
        self.set_identity_geometry(width=width, height=height)
        outputs = self.forward(nv12)
        return self.post_process(outputs)


def draw_detection(img: np.ndarray, bbox: tuple[int, int, int, int], score: float, class_id: int) -> None:
    x1, y1, x2, y2 = bbox
    color = RDK_COLORS[class_id % len(RDK_COLORS)]
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

    label = f"cls_id={class_id}: {score:.2f}"
    (label_w, label_h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    label_x, label_y = x1, y1 - 10 if y1 - 10 > label_h else y1 + 10

    cv2.rectangle(
        img,
        (label_x, label_y - label_h),
        (label_x + label_w, label_y + label_h),
        color,
        cv2.FILLED,
    )
    cv2.putText(img, label, (label_x, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
