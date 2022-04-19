import time
import torch
import torch.nn.functional as F
from torchvision import transforms
from torch.nn.functional import pad
from segmentor.segmentor import SEGMENTOR_REGISTRY, Segmentor
from model_zoo.MiVOS.model.eval_network import PropagationNetwork
from model_zoo.MiVOS.inference_core import InferenceCore
from model_zoo.MiVOS.inference_core_yv import InferenceCore as InferenceCoreYV
from utils import all_to_onehot
import numpy as np
import os
from PIL import Image
@SEGMENTOR_REGISTRY.register()
class MIVOS_DV16(Segmentor):
    def __init__(self, cfg):
        self.name = "MIVOS_DV16"
        print("Initializing MIVOS segmentor for DAVIS 16.")
        top_k = int(cfg['top_k'])
        if cfg["use_km"] == 1:
            km = 5.6
        else:
            km = None
        self.mem_every = int(cfg['mem_every'])

        self.prop_model = PropagationNetwork(top_k=top_k, km=km).cuda().eval()
        prop_saved = torch.load(cfg['model'])
        self.prop_model.load_state_dict(prop_saved)

    def build_dataset(self, resolution="480p", single_object=False):
        self.single_object = single_object
        self.resolution = resolution

    def inference(
        self,
        all_rgb,
        all_rgb_tensor,
        key_idx,
        msk_folder,
        feature_extractor=None,
        **kwargs
    ):
        labels = [1]
        mask_file = os.path.join(msk_folder, "00000.png")
        masks = np.expand_dims(np.array(Image.open(mask_file).convert('P'), dtype=np.uint8),0)
        masks = (masks > 0.5).astype(np.uint8)
        msk = torch.from_numpy(all_to_onehot(masks, labels)).float()
        msk = msk.unsqueeze(0)
        rgb = all_rgb_tensor[key_idx].unsqueeze(0)
        torch.cuda.synchronize()
        timeStarted = time.time()
        processor = InferenceCore(self.prop_model, rgb, 1, mem_freq=self.mem_every)
        processor.interact(msk[:,0], 0, rgb.shape[1])

        # Do unpad -> upsample to original size 
        out_masks = torch.zeros((processor.t, 1, *rgb.shape[-2:]), dtype=torch.int64, device="cuda")
        out_pred = torch.zeros((processor.t, 1, processor.nh // 4, processor.nw // 4),
            dtype=torch.int64,
            device="cuda",
        )
        for ti in range(processor.t):
            prob = processor.prob[:,ti]
            prob4 = F.interpolate(
                prob,
                (processor.nh // 4, processor.nw // 4),
                mode="bilinear",
                align_corners=False,
            )


            if processor.pad[2]+processor.pad[3] > 0:
                prob = prob[:,:,processor.pad[2]:-processor.pad[3],:]
            if processor.pad[0]+processor.pad[1] > 0:
                prob = prob[:,:,:,processor.pad[0]:-processor.pad[1]]
            
            out_pred[ti] = torch.argmax(prob4, dim=0)
            out_masks[ti] = torch.argmax(prob, dim=0)

        out_masks = out_masks.detach()


        torch.cuda.synchronize()
        segment_time = time.time() - timeStarted

        torch.cuda.synchronize()
        OverheadTimeStarted = time.time()

        out_pred = F.one_hot(out_pred).squeeze(1).permute(0, 3, 1, 2)
        out_pred = out_pred[:, 1:]#exclude background
        low_feat = processor.low_feat
        pad = processor.pad

        torch.cuda.synchronize()
        overhead_time = time.time() - OverheadTimeStarted


        print(
            "Segment {}x{} video at FPS {:.2f}.".format(
                rgb.shape[-2], rgb.shape[-1], rgb.shape[1] / segment_time
            )
        )
        del rgb
        del msk
        del processor
        torch.cuda.empty_cache()
        return (
            out_masks,
            out_pred,
            low_feat,
            pad,
            segment_time,
            overhead_time,
        )

    def low_level_extractor(self, x):

        n = self.prop_model.rgb_encoder
        x = n.conv1(x)
        x = n.bn1(x)
        x = n.relu(x)  # 1/2, 64
        x = n.maxpool(x)  # 1/4, 64
        f4 = n.res2(x)  # 1/4, 256

        return f4



@SEGMENTOR_REGISTRY.register()
class MIVOS_DV17(Segmentor):
    def __init__(self, cfg):
        self.name = "MIVOS_DV16"
        print("Initializing MIVOS segmentor for DAVIS 17.")
        top_k = int(cfg['top_k'])
        if cfg["use_km"] == 1:
            km = 5.6
        else:
            km = None
        self.mem_every = int(cfg['mem_every'])

        self.prop_model = PropagationNetwork(top_k=top_k, km=km).cuda().eval()
        prop_saved = torch.load(cfg['model'])
        self.prop_model.load_state_dict(prop_saved)

    def build_dataset(self, resolution="480p", single_object=False):
        self.single_object = single_object
        self.resolution = resolution

    def inference(
        self,
        all_rgb,
        all_rgb_tensor,
        key_idx,
        msk_folder,
        feature_extractor=None,
        **kwargs
    ):
        mask_file = os.path.join(msk_folder, "00000.png")
        _mask = np.array(Image.open(mask_file).convert("P"), dtype=np.uint8)

        images = []
        masks = []
        for f in key_idx:
            images.append(all_rgb_tensor[f])
            if f == 0:
                masks.append(_mask)
            else:
                masks.append(np.zeros_like(masks[0]))

        images = torch.stack(images, 0)
        masks = np.stack(masks, 0)

        if self.single_object:
            labels = [1]
            masks = (masks > 0.5).astype(np.uint8)
            masks = torch.from_numpy(all_to_onehot(masks, labels)).float()
        else:
            labels = np.unique(masks[0])
            labels = labels[labels != 0]
            masks = torch.from_numpy(all_to_onehot(masks, labels)).float()

        masks = masks.unsqueeze(2)

        rgb = images.cuda().unsqueeze(0)
        msk = masks.cuda()
        k = len(labels)
        torch.cuda.synchronize()
        timeStarted = time.time()
        processor = InferenceCore(self.prop_model, rgb, k, mem_freq=self.mem_every)
        processor.interact(msk[:,0], 0, rgb.shape[1])

        # Do unpad -> upsample to original size 
        out_masks = torch.zeros((processor.t, 1, *rgb.shape[-2:]), dtype=torch.int64, device="cuda")
        out_pred = torch.zeros((processor.t, 1, processor.nh // 4, processor.nw // 4),
            dtype=torch.int64,
            device="cuda",
        )
        for ti in range(processor.t):
            prob = processor.prob[:,ti]
            prob4 = F.interpolate(
                prob,
                (processor.nh // 4, processor.nw // 4),
                mode="bilinear",
                align_corners=False,
            )
            if processor.pad[2]+processor.pad[3] > 0:
                prob = prob[:,:,processor.pad[2]:-processor.pad[3],:]
            if processor.pad[0]+processor.pad[1] > 0:
                prob = prob[:,:,:,processor.pad[0]:-processor.pad[1]]
            
            out_pred[ti] = torch.argmax(prob4, dim=0)
            out_masks[ti] = torch.argmax(prob, dim=0)

        out_masks = out_masks.detach()

        torch.cuda.synchronize()
        segment_time = time.time() - timeStarted


        torch.cuda.synchronize()
        OverheadTimeStarted = time.time()
        out_pred = F.one_hot(out_pred).squeeze(1).permute(0, 3, 1, 2)
        out_pred = out_pred[:, 1:]#exclude background

        # extract low level features using stand-along feature extractor
        processor.low_feat[0]=feature_extractor(processor.images[:,0])
        low_feat = processor.low_feat
        pad = processor.pad
        torch.cuda.synchronize()
        overhead_time = time.time() - OverheadTimeStarted


        print(
            "Segment {}x{} video at FPS {:.2f}.".format(
                rgb.shape[-2], rgb.shape[-1], rgb.shape[1] / segment_time
            )
        )
        del rgb
        del msk
        del processor

        return (
            out_masks,
            out_pred,
            low_feat,
            pad,
            segment_time,
            overhead_time,
        )
    def low_level_extractor(self, x):

        n = self.prop_model.rgb_encoder
        x = n.conv1(x)
        x = n.bn1(x)
        x = n.relu(x)  # 1/2, 64
        x = n.maxpool(x)  # 1/4, 64
        f4 = n.res2(x)  # 1/4, 256

        return f4




@SEGMENTOR_REGISTRY.register()
class MIVOS_YTVOS(Segmentor):
    def __init__(self, cfg):
        self.name = "MIVOS_YTVOS"
        print("Initializing MIVOS segmentor for YTVOS.")
        top_k = int(cfg['top_k'])
        if cfg["use_km"] == 1:
            km = 5.6
        else:
            km = None
        self.mem_every = int(cfg['mem_every'])

        self.prop_model = PropagationNetwork(top_k=top_k, km=km).cuda().eval()
        prop_saved = torch.load(cfg['model'])
        self.prop_model.load_state_dict(prop_saved)

    def build_dataset(self, resolution="480p", single_object=False):
        self.single_object = single_object
        self.resolution = resolution


        self.mask_transform = transforms.Compose(
            [
                transforms.Resize(480, interpolation=transforms.InterpolationMode.NEAREST),
            ]
        )

    def all_to_onehot(self, masks, labels):
        Ms = np.zeros(
            (len(labels), masks.shape[0], masks.shape[1], masks.shape[2]),
            dtype=np.uint8,
        )
        for k, l in enumerate(labels):
            Ms[k] = (masks == l).astype(np.uint8)
        return Ms

    def inference(
        self,
        all_rgb,
        rgb_all_frames,
        key_frame_indexes,
        vid_gt_path,
        feature_extractor=None,
        mem_freq=5,
        base_index=None,
    ):

        info = {}
        info["num_objects"] = 0
        first_mask = os.listdir(vid_gt_path)[0]
        _mask = np.array(Image.open(os.path.join(vid_gt_path, first_mask)).convert("P"))
        size = np.shape(_mask)
        gt_obj = {}  # Frames with labelled objects
        base_index = int(base_index)

        images = []
        masks = []
        this_label_history = None
        for i, f in enumerate(rgb_all_frames):
            images.append(f)

            mask_file = os.path.join(vid_gt_path, "{:05d}.png".format(i + base_index))
            if os.path.exists(mask_file):
                masks.append(
                    np.array(Image.open(mask_file).convert("P"), dtype=np.uint8)
                )
                this_labels = np.unique(masks[-1])
                this_labels = this_labels[this_labels != 0]
                if np.all(this_labels == this_label_history):
                    pass
                else:
                    gt_obj[i] = this_labels
                    this_label_history = this_labels
                    if i not in key_frame_indexes:
                        next_key_frame = key_frame_indexes[
                            np.argmax(key_frame_indexes > i)
                        ]
                        key_frame_indexes = np.sort(
                            np.append(key_frame_indexes, np.arange(i, next_key_frame))
                        )# TODO: problem.. should move this outside
            else:
                # Mask not exists -> nothing in it
                masks.append(np.zeros(size))

        for key in list(gt_obj):
            gt_obj[np.where(key_frame_indexes == key)[0][0]] = gt_obj.pop(key)

        images = torch.stack(images, 0)[key_frame_indexes]
        masks = np.stack(masks, 0)[key_frame_indexes]
        num_frames = key_frame_indexes.shape[0]
        torch.cuda.empty_cache()

        # Construct the forward and backward mapping table for labels
        labels = np.unique(masks).astype(np.uint8)
        labels = labels[labels != 0]
        info["label_convert"] = {}
        info["label_backward"] = {}
        idx = 1
        for l in labels:
            info["label_convert"][l] = idx
            info["label_backward"][idx] = l
            idx += 1
        masks = torch.from_numpy(self.all_to_onehot(masks, labels)).float()

        # Resize to 480p
        masks = self.mask_transform(masks)
        masks = masks.unsqueeze(2)

        rgb = images.unsqueeze(0)
        msk = masks

        k = len(labels)
        torch.cuda.empty_cache()

        torch.cuda.synchronize()
        timeStarted = time.time()

        # Frames with labels, but they are not exhaustively labeled
        frames_with_gt = sorted(list(gt_obj.keys()))
        processor = InferenceCoreYV(
            self.prop_model, rgb, num_objects=k, mem_freq=self.mem_every
        )
        # min_idx tells us the starting point of propagation
        # Propagating before there are labels is not useful
        min_idx = 99999
        for i, frame_idx in enumerate(frames_with_gt):
            min_idx = min(frame_idx, min_idx)
            # Note that there might be more than one label per frame
            obj_idx = gt_obj[frame_idx].tolist()
            # Map the possibly non-continuous labels into a continuous scheme

            obj_idx = [info["label_convert"][o] for o in obj_idx]

            # Append the background label
            with_bg_msk = torch.cat(
                [
                    1 - torch.sum(msk[:, frame_idx], dim=0, keepdim=True),
                    msk[:, frame_idx],
                ],
                0,
            ).cuda()

            # We perform propagation from the current frame to the next frame with label
            if i == len(frames_with_gt) - 1:
                processor.interact(with_bg_msk, frame_idx, rgb.shape[1], obj_idx)
            else:
                processor.interact(
                    with_bg_msk, frame_idx, frames_with_gt[i + 1] + 1, obj_idx
                )

        # Do unpad -> upsample to original size (we made it 480p)
        out_pred = torch.zeros(
            (processor.t, k+1, 1, *rgb.shape[-2:]), dtype=torch.float32, device="cuda"
        )
        out_pred4 = torch.zeros((processor.t, 1, processor.nh // 4, processor.nw // 4),
            dtype=torch.int64,
            device="cuda",
        )
        for ti in range(processor.t):
            prob = processor.prob[:, ti]
            prob4 = F.interpolate(
                prob,
                (processor.nh // 4, processor.nw // 4),
                mode="bilinear",
                align_corners=False,
            )
            if processor.pad[2] + processor.pad[3] > 0:
                prob = prob[:, :, processor.pad[2] : -processor.pad[3], :]
            if processor.pad[0] + processor.pad[1] > 0:
                prob = prob[:, :, :, processor.pad[0] : -processor.pad[1]]

            #prob = F.interpolate(prob, size, mode="bilinear", align_corners=False)
            out_pred4[ti] = torch.argmax(prob4, dim=0)
            out_pred[ti] = prob
        
        idx_mask = torch.zeros_like(out_pred4)
        for obj_idx in range(1, k+1):
            backward_idx = info['label_backward'][obj_idx].item()
            idx_mask[out_pred4==obj_idx] = backward_idx

        out_pred4 = idx_mask.detach()

        # Remap the indices to the original domain
        # idx_masks = np.zeros_like(out_masks)
        # for i in range(1, k+1):
        #     backward_idx = info['label_backward'][i].item()
        #     idx_masks[out_masks==i] = backward_idx

        torch.cuda.synchronize()
        segment_time = (time.time() - timeStarted)

        print(
            "Segment {}x{} video at FPS {:.2f}.".format(
                size[1], size[0], num_frames / segment_time
            )
        )

        out_pred4 = F.one_hot(out_pred4).squeeze(1).permute(0, 3, 1, 2)
        #exclude background
        out_pred4 = out_pred4[:, 1:]
        feats = processor.low_feat
        pad = processor.pad
        del rgb
        del msk
        del processor
        torch.cuda.empty_cache()
        return (
            key_frame_indexes,
            out_pred,
            out_pred4,
            feats,
            size,
            info['label_backward'],
            k,
            pad,
            segment_time,
            0,
        )
    def low_level_extractor(self, x):

        n = self.prop_model.rgb_encoder
        x = n.conv1(x)
        x = n.bn1(x)
        x = n.relu(x)  # 1/2, 64
        x = n.maxpool(x)  # 1/4, 64
        f4 = n.res2(x)  # 1/4, 256

        return f4