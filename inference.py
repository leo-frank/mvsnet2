import os
import torch
from torch import nn
import torch.nn.functional as F
from torchvision import transforms
from torchvision.datasets import MNIST
from torch.utils.data import DataLoader
import pytorch_lightning as pl
import cv2
from dtu_data import DTU_dataset

# Define torch modules
class FeatureExtractor(nn.Module):
    def __init__(self):
        super().__init__()
        # NOTICE: I wanna keep feature map as the same size with input
        self.encoder = nn.Conv2d(in_channels=3, out_channels=8, kernel_size=3, padding=1)
    def forward(self, image):
        # argument 'image' contains only 1 image
        return self.encoder(image)

class VolumeReg(nn.Module):
    def __init__(self):
        super().__init__()
        self.reg = nn.Conv3d(8, 1, 3)
    def forward(self, cost_volume):
        return self.reg(cost_volume)

# Input:
# features: a list of feature; 
# features[0]: Tensor [batch_size, channels, H, W]
# intrinsic: Tensor [batch_size, 3, 3]
# extrinsic: Tensor [batch_size, 4, 4]
# depth_values: Tensor [batch_size, N], each sample is a list of valid depths; depth_values[0][0]: int
# Output:
# cost_volume: [batch_size, D, channels, H, W]
def homography_mapping(features, extrinsics, intrinsics, depth_values, nviews):
    ref_feature, src_features = features[0], features[1:]
    ref_proj, src_projs = extrinsics[0], extrinsics[1:]
    ref_intrinsic = intrinsics[0]
    ref_rotation, ref_trans = ref_proj[:, :3, :3], ref_proj[:, :3, 3]
    batch_size, channels_num, H, W = ref_feature.shape[0], ref_feature.shape[1], ref_feature.shape[2], ref_feature.shape[3]
    # FIXME: ref volume
    volume = torch.empty([batch_size, len(depth_values), channels_num, H, W]).to(ref_feature.device)
    volume_sum = torch.empty([batch_size, len(depth_values), channels_num, H, W]).to(ref_feature.device)
    volume_square = torch.empty([batch_size, len(depth_values), channels_num, H, W]).to(ref_feature.device)
    for src_fea, src_proj in zip(src_features, src_projs):
        # src_rotation: [batch_size, 3, 3], src_trans: [batch_size, 3, 1]
        src_rotation, src_trans = src_proj[:, :3, :3], src_proj[:, :3, 3]
        relative_rotation = torch.matmul(torch.inverse(src_rotation), ref_rotation)
        relative_trans = ref_trans - src_trans
        normal_T = torch.transpose(torch.Tensor([[0, 0, 1]]), 0, 1).unsqueeze(0).to(ref_feature.device)
        print("relative_rotation.shape: {}".format(relative_rotation.shape))
        print("relative_rotation.device: {}".format(relative_rotation.device))
        print("normal_T.device: {}".format(normal_T.device))
        print("volume.device: {}".format(volume.device))
        
        i = 0
        for depth in depth_values[0]:
            src_intrinsic = intrinsics[i]
            Homo = torch.matmul(torch.matmul(src_intrinsic, relative_rotation + torch.transpose(relative_rotation, 1, 2) * relative_trans * normal_T / depth), torch.inverse(ref_intrinsic))
            print("Homo", Homo.shape) # the shape of Homo should be [3, 3], freedom of 8
            grid = torch.empty(batch_size, H, W, 2).to(ref_feature.device)
            for x in range(H):
                print(x)
                for y in range(W):
                    tmp = Homo[:, 2, 0]*x + Homo[:, 2, 1]*y + Homo[:, 2, 2]
                    u, v = (Homo[:, 0, 0]*x + Homo[:, 0, 1]*y + Homo[:, 0, 2]) / tmp , (Homo[:, 1, 0]*x + Homo[:, 1, 1]*y + Homo[:, 1, 2]) / tmp
                    u_norm, v_norm = 2 * u / H - 1, 2 * v / W - 1
                    grid[:, x, y] = torch.Tensor([u_norm , v_norm])
            output = F.grid_sample(src_fea, grid)
            volume[:, i] = output
            i += 1
        volume_sum += volume
        volume_square += volume.pow(2)
    cost_volume = volume_square / nviews - (volume_sum / nviews).pow(2)
    return cost_volume

# probility_volume: [batch_size, D, H, W]
# depth_values: [batch_size, D]
def depth_regression(probility_volume, depth_values):
    d = torch.Tensor(depth_values).view(probility_volume.shape[0], probility_volume.shape[1], 1, 1)
    depth = torch.sum(d * probility_volume, 1)
    return depth

# convert a function into recursive style to handle nested dict/list/tuple variables
def make_recursive_func(func):
    def wrapper(vars):
        if isinstance(vars, list):
            return [wrapper(x) for x in vars]
        elif isinstance(vars, tuple):
            return tuple([wrapper(x) for x in vars])
        elif isinstance(vars, dict):
            return {k: wrapper(v) for k, v in vars.items()}
        else:
            return func(vars)

    return wrapper

@make_recursive_func
def tocuda(vars):
    if isinstance(vars, torch.Tensor):
        return vars.cuda()
    elif isinstance(vars, str):
        return vars
    else:
        raise NotImplementedError("invalid input type {} for tensor2numpy".format(type(vars)))


# Define a LightningModule
class MVSVolume(pl.LightningModule):
    def __init__(self):
        super().__init__()
        self.FeatureExtractor = FeatureExtractor()
        self.VolumeReg = VolumeReg()

    # batch consists of these:
    #   imgs: [batch_size, nviews, 3, 640, 512]
    #   intrinsics: [batch_size, nviews, 3, 3]
    #   extrinsics: [batch_size, nviews, 3, 3]
    #   depth_gt: [batch_size, 160, 128]
    def training_step(self, batch, batch_idx):
        # print(batch)
        imgs, intrinsics, extrinsics, depth_gt, depth_values= list(tocuda(batch).values())
        imgs = torch.unbind(imgs, 1) # imgs is a tuple of neighbor imgs [batch_size, 3, H, W], length N
        intrinsics = torch.unbind(intrinsics, 1)
        extrinsics = torch.unbind(extrinsics, 1)
        features = [self.FeatureExtractor(i) for i in imgs]
        nviews = len(imgs)
        cost_volume = homography_mapping(features, extrinsics, intrinsics, depth_values, nviews)
        probility_volume = F.softmax(self.VolumeReg(cost_volume))
        depth_map = depth_regression(probility_volume)
        loss = F.mse_loss(depth_map, depth_gt)
        return loss
    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer
    def validation_step():
        pass
    def test_step():
        pass

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Define a dataset
batch_size = 1
dataset = DTU_dataset(data_dir="./dtu_train/", mode='train', nviews=3)
train_loader = DataLoader(dataset, batch_size)

# model
model = MVSVolume()

model.to(device)
 
# Train the model
if __name__ == '__main__':
    # imgs, intrinsics, extrinsics, ref_depth = dataset[0]
    trainer = pl.Trainer(limit_train_batches=100, max_epochs=1, gpus=1)
    trainer.fit(model, train_dataloader=train_loader)