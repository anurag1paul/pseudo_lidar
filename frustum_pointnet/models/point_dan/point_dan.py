import torch

from models.point_dan.model_utils import *
from torch import nn

# Channel Attention
from models.utils import create_mlp_components


class CALayer(nn.Module):
    def __init__(self, channel, reduction=8):
        super(CALayer, self).__init__()
        # feature channel downscale and upscale --> channel weight
        self.conv_du = nn.Sequential(
            nn.Conv2d(channel, channel // reduction, 1, padding=0, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel // reduction, channel, 1, padding=0, bias=True),
            nn.Sigmoid()
        )
        self.bn = nn.BatchNorm1d(4096)

    def forward(self, x):
        y = self.conv_du(x)
        y = x * y + x
        y = y.view(y.shape[0], -1)
        y = self.bn(y)

        return y


# Grad Reversal
class GradReverse(torch.autograd.Function):
    def __init__(self, lambd):
        self.lambd = lambd

    def forward(self, x):
        return x.view_as(x)

    def backward(self, grad_output):
        return grad_output * -self.lambd


def grad_reverse(x, lambd=1.0):
    return GradReverse(lambd)(x)


# Generator
class PointnetG(nn.Module):
    def __init__(self):
        super(PointnetG, self).__init__()
        self.trans_net1 = transform_net(3, 3)
        self.trans_net2 = transform_net(64, 64)
        self.conv1 = conv_2d(3, 64, 1)
        self.conv2 = conv_2d(64, 64, 1)
        # SA Node Module
        self.conv3 = adapt_layer_off()  # (64->128)
        self.conv4 = conv_2d(128, 128, 1)
        self.conv5 = conv_2d(128, 1024, 1)
        self.bn1 = nn.BatchNorm1d(1024)

    def forward(self, x, node=False):
        x_loc = x.squeeze(-1)

        transform = self.trans_net1(x)
        x = x.transpose(2, 1)

        x = x.squeeze(-1)
        x = torch.bmm(x, transform)
        x = x.unsqueeze(3)
        x = x.transpose(2, 1)
        x = self.conv1(x)
        x = self.conv2(x)
        transform = self.trans_net2(x)
        x = x.transpose(2, 1)
        x = x.squeeze(-1)
        x = torch.bmm(x, transform)
        point_feat = x

        x = x.unsqueeze(3)
        x = x.transpose(2, 1)
        x, node_feat, node_off = self.conv3(x, x_loc)
        # x = [B, dim, num_node, 1]/[64, 64, 1024, 1]; x_loc = [B, xyz, num_node] / [64, 3, 1024]

        x = self.conv4(x)
        x = self.conv5(x)

        x, _ = torch.max(x, dim=2, keepdim=False)
        x = x.squeeze(-1)
        x = self.bn1(x)

        cloud_feat = x

        if node:
            return cloud_feat, point_feat, node_feat, node_off
        else:
            return cloud_feat, point_feat, node_feat


class InstanceSegmentationPointDAN(nn.Module):

    def __init__(self, num_classes=3, extra_feature_channels=1, width_multiplier=1):
        super(InstanceSegmentationPointDAN, self).__init__()
        self.in_channels = extra_feature_channels + 3
        self.num_classes = num_classes

        self.g = PointnetG()

        self.attention_s = CALayer(64 * 64)
        self.attention_t = CALayer(64 * 64)

        channels_point = 64
        channels_cloud = 1024

        layers, _ = create_mlp_components(
            in_channels=(channels_point + channels_cloud + self.num_classes),
            out_channels=[512, 256, 128, 128, 0.5, 2],
            classifier=True, dim=2, width_multiplier=1
        )
        self.c1 = nn.Sequential(*layers)

        layers, _ = create_mlp_components(
            in_channels=(channels_point + channels_cloud + self.num_classes),
            out_channels=[512, 256, 128, 128, 0.5, 2],
            classifier=True, dim=2, width_multiplier=1
        )
        self.c2 = nn.Sequential(*layers)

    def forward(self, inputs, constant=1, adaptation=False, node_vis=False,
                node_adaptation_s=False, node_adaptation_t=False):

        features = inputs['features']
        num_points = features.size(-1)
        one_hot_vectors = inputs['one_hot_vectors'].unsqueeze(-1).repeat(
            [1, 1, num_points])

        assert one_hot_vectors.dim() == 3  # [B, C, N]

        cloud_feat, point_feat, feat_ori, node_idx = self.g(features, node=True)
        batch_size = feat_ori.size(0)

        # sa node visualization
        if node_vis:
            return node_idx

        if node_adaptation_s:
            # source domain sa node feat
            feat_node = feat_ori.view(batch_size, -1)
            feat_node_s = self.attention_s(feat_node.unsqueeze(2).unsqueeze(3))
            return feat_node_s
        elif node_adaptation_t:
            # target domain sa node feat
            feat_node = feat_ori.view(batch_size, -1)
            feat_node_t = self.attention_t(feat_node.unsqueeze(2).unsqueeze(3))
            return feat_node_t

        if adaptation:
            cloud_feat = grad_reverse(cloud_feat, constant)

        cls_input = torch.cat([one_hot_vectors, point_feat, cloud_feat], dim=1)

        y1 = self.c1(cls_input)
        y2 = self.c2(cls_input)

        return y1, y2