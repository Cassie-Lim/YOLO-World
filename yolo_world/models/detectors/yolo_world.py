# Copyright (c) Tencent Inc. All rights reserved.
from typing import List, Tuple, Union
from numpy import isin
import torch
import torch.nn as nn
from torch import Tensor
from mmdet.structures import OptSampleList, SampleList
from mmyolo.models.detectors import YOLODetector
from mmyolo.registry import MODELS


@MODELS.register_module()
class YOLOWorldDetector(YOLODetector):
    """Implementation of YOLOW Series"""

    def __init__(self,
                 *args,
                 mm_neck: bool = False,
                 num_train_classes=80,
                 num_test_classes=80,
                 **kwargs) -> None:
        self.mm_neck = mm_neck
        self.num_train_classes = num_train_classes
        self.num_test_classes = num_test_classes
        super().__init__(*args, **kwargs)

    def loss(self, batch_inputs: Tensor,
             batch_data_samples: SampleList) -> Union[dict, list]:
        """Calculate losses from a batch of inputs and data samples."""
        self.bbox_head.num_classes = self.num_train_classes
        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)
        losses = self.bbox_head.loss(img_feats, txt_feats, batch_data_samples)
        return losses

    def predict(self,
                batch_inputs: Tensor,
                batch_data_samples: SampleList,
                rescale: bool = True) -> SampleList:
        """Predict results from a batch of inputs and data samples with post-
        processing.
        """

        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)

        # self.bbox_head.num_classes = self.num_test_classes
        self.bbox_head.num_classes = txt_feats[0].shape[0]
        results_list = self.bbox_head.predict(img_feats,
                                              txt_feats,
                                              batch_data_samples,
                                              rescale=rescale)

        batch_data_samples = self.add_pred_to_datasample(
            batch_data_samples, results_list)
        return batch_data_samples

    def reparameterize(self, texts: List[List[str]]) -> None:
        # encode text embeddings into the detector
        self.texts = texts
        self.text_feats = self.backbone.forward_text(texts)

    def _forward(
            self,
            batch_inputs: Tensor,
            batch_data_samples: OptSampleList = None) -> Tuple[List[Tensor]]:
        """Network forward process. Usually includes backbone, neck and head
        forward without any post-processing.
        """
        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)
        results = self.bbox_head.forward(img_feats, txt_feats)
        return results

    def extract_feat(
            self, batch_inputs: Tensor,
            batch_data_samples: SampleList) -> Tuple[Tuple[Tensor], Tensor]:
        """Extract features."""
        txt_feats = None
        if batch_data_samples is None:
            texts = self.texts
            txt_feats = self.text_feats
        elif isinstance(batch_data_samples, dict) and 'texts' in batch_data_samples:
            texts = batch_data_samples['texts']
        elif isinstance(batch_data_samples, list) and hasattr(batch_data_samples[0], 'texts'):
            texts = [data_sample.texts for data_sample in batch_data_samples]
        elif hasattr(self, 'text_feats'):
            texts = self.texts
            txt_feats = self.text_feats
        else:
            raise TypeError('batch_data_samples should be dict or list.')
        if txt_feats is not None:
            # forward image only
            img_feats = self.backbone.forward_image(batch_inputs)
        else:
            img_feats, txt_feats = self.backbone(batch_inputs, texts)
        if self.with_neck:
            if self.mm_neck:
                img_feats = self.neck(img_feats, txt_feats)
            else:
                img_feats = self.neck(img_feats)
        return img_feats, txt_feats
    def query_cls_embed(self, texts, cls_embed, scale_logits=None, consider_uncertainty=False, pre_normalized=True):
        '''
        Args:
            texts: Input texts associated with the classes. [or image features]
            cls_embed: Tensor of shape [1, 512, h, w] representing class embeddings for each pixel.
        Returns:
            scores, labels: Both are numpy arrays where scores give the confidence and
            labels provide the class label, both with the shape [h, w].
        '''
        if isinstance(texts, list):
            assert isinstance(texts[0], str)
            txt_feats = self.backbone.forward_text([texts]) # [1, num_classes, 512]
        else:
            txt_feats = texts.reshape(-1, 1, texts.shape[-1])
        cls_logits = []
        for cls_contrast in self.bbox_head.head_module.cls_contrasts:
            # Expecting [1, num_classes, h, w]
            # cls_logit = cls_contrast.forward(cls_embed, txt_feats)
            if pre_normalized:
                cls_logit = cls_contrast.forward_no_normalization(cls_embed, txt_feats)
            else:
                cls_logit = cls_contrast.forward_flattened(cls_embed, txt_feats)
            cls_logits.append(cls_logit.sigmoid())
        # cls_logits = torch.stack(cls_logits, dim=0)
        # stds = cls_logits.std(dim=(1,2,3))
        # cls_logit = cls_logits[stds.argmax()].squeeze(0)
        # Average logits across different contrasts
        # cls_logit = cls_logits[1].squeeze(0)
        if scale_logits is not None:
            supvervised_mask = scale_logits[:, -1] < 0.5
            scale_logits = scale_logits[:, :-1] # remove uncertainty channel
            stacked_cls_logits = torch.stack(cls_logits, dim=0).permute(2, 0, 1)    # [num_contrasts, num_classes, num_vertexs] -> [num_vertexs, num_contrasts, num_classes]
            scale_logits[supvervised_mask] = scale_logits[supvervised_mask] / (scale_logits[supvervised_mask].sum(dim=1, keepdim=True) + 1e-12) # normalize for supervised region
            cls_logit = (stacked_cls_logits * scale_logits[..., None]).sum(dim=1).permute(1, 0) # [num_vertexs, num_classes] -> [num_classes, num_vertexs]
            # scale_logits = scale_logits.permute(1, 0).unsqueeze(1)
            # cls_logit = (torch.stack(cls_logits, dim=0) * scale_logits).sum(dim=0)
        else:
            cls_logit = torch.stack(cls_logits, dim=0).mean(dim=0) # Shape should be [num_classes, h, w]
        
        # Compute max along classes dimension to find the class label with highest confidence per pixel
        scores, labels = torch.max(cls_logit, dim=0)  # Now scores and labels should both have shape [h, w]
        
        return scores, labels, cls_logit
        # return scores.cpu().numpy(), labels.cpu().numpy()

        


@MODELS.register_module()
class YOLOWorldPromptDetector(YOLODetector):
    """Implementation of YOLO World Series"""

    def __init__(self,
                 *args,
                 mm_neck: bool = False,
                 num_train_classes=80,
                 num_test_classes=80,
                 prompt_dim=512,
                 num_prompts=80,
                 embedding_path='',
                 freeze_prompt=False,
                 use_mlp_adapter=False,
                 **kwargs) -> None:
        self.mm_neck = mm_neck
        self.num_training_classes = num_train_classes
        self.num_test_classes = num_test_classes
        self.prompt_dim = prompt_dim
        self.num_prompts = num_prompts
        self.freeze_prompt = freeze_prompt
        self.use_mlp_adapter = use_mlp_adapter
        super().__init__(*args, **kwargs)

        if len(embedding_path) > 0:
            import numpy as np
            self.embeddings = torch.nn.Parameter(
                torch.from_numpy(np.load(embedding_path)).float())
        else:
            # random init
            embeddings = nn.functional.normalize(
                torch.randn((num_prompts, prompt_dim)),dim=-1)
            self.embeddings = nn.Parameter(embeddings)

        if self.freeze_prompt:
            self.embeddings.requires_grad = False
        else:
            self.embeddings.requires_grad = True

        if use_mlp_adapter:
            self.adapter = nn.Sequential(nn.Linear(prompt_dim, prompt_dim * 2),
                                         nn.ReLU(True),
                                         nn.Linear(prompt_dim * 2, prompt_dim))
        else:
            self.adapter = None

    def loss(self, batch_inputs: Tensor,
             batch_data_samples: SampleList) -> Union[dict, list]:
        """Calculate losses from a batch of inputs and data samples."""
        self.bbox_head.num_classes = self.num_training_classes
        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)
        losses = self.bbox_head.loss(img_feats, txt_feats, batch_data_samples)
        return losses

    def predict(self,
                batch_inputs: Tensor,
                batch_data_samples: SampleList,
                rescale: bool = True) -> SampleList:
        """Predict results from a batch of inputs and data samples with post-
        processing.
        """

        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)

        self.bbox_head.num_classes = self.num_test_classes
        results_list = self.bbox_head.predict(img_feats,
                                              txt_feats,
                                              batch_data_samples,
                                              rescale=rescale)

        batch_data_samples = self.add_pred_to_datasample(
            batch_data_samples, results_list)
        return batch_data_samples

    def _forward(
            self,
            batch_inputs: Tensor,
            batch_data_samples: OptSampleList = None) -> Tuple[List[Tensor]]:
        """Network forward process. Usually includes backbone, neck and head
        forward without any post-processing.
        """
        img_feats, txt_feats = self.extract_feat(batch_inputs,
                                                 batch_data_samples)
        results = self.bbox_head.forward(img_feats, txt_feats)
        return results

    def extract_feat(
            self, batch_inputs: Tensor,
            batch_data_samples: SampleList) -> Tuple[Tuple[Tensor], Tensor]:
        """Extract features."""
        # only image features
        img_feats, _ = self.backbone(batch_inputs, None)
        # use embeddings
        txt_feats = self.embeddings[None]
        if self.adapter is not None:
            txt_feats = self.adapter(txt_feats) + txt_feats
            txt_feats = nn.functional.normalize(txt_feats, dim=-1, p=2)
        txt_feats = txt_feats.repeat(img_feats[0].shape[0], 1, 1)

        if self.with_neck:
            if self.mm_neck:
                img_feats = self.neck(img_feats, txt_feats)
            else:
                img_feats = self.neck(img_feats)
        return img_feats, txt_feats
