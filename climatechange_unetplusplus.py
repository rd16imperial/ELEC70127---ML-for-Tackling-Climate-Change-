# -*- coding: utf-8 -*-
"""ClimateChange_UNetPlusPlus.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/15JfG_16KGWLutFBRBbv3iyYwCnawG68Z
"""

# Install rasterio library
!pip install rasterio

from google.colab import drive #Mount google drive
drive.mount('/content/drive')

import torch                                        #for PyTorch
import torch.nn as nn                               #for neural networks
import torch.nn.functional as F
import rasterio                                     #for raster data handling
import numpy as np                                  #for numerical operations
import pandas as pd                                 #for data manipulation
from torch.optim.lr_scheduler import ReduceLROnPlateau #for learning rate scheduling
import os                                          #for file system operations
import torch.optim as optim

from torch.utils.data import Dataset, DataLoader   #for data loading
import matplotlib.pyplot as plt                    #For plotting
from tqdm import tqdm                              #For creating progress bars during loops
import shutil                                      #for file operations
import math                                        #for mathematical functions
from scipy import ndimage                          #for multidimensional image processing

class NestedConvBlock(nn.Module):
    """
    A nested convolutional block with two 3x3 convolutions, each followed by
    batch normalization and ReLU activation.
    """
    def __init__(self, in_channels, out_channels):
        super(NestedConvBlock, self).__init__()
        # First 3x3 convolution + BN + ReLU
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
        # Second 3x3 convolution + BN
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        # Forward pass through both convolutional layers with activations
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.relu(self.bn2(self.conv2(x)))
        return x

class UNetPlusPlus(nn.Module):
    """
    UNet++: A nested U-Net architecture with deep supervision and densely connected skip pathways.

    Features:
      - Hierarchical encoder with downsampling at each stage.
      - Nested decoder with progressively refined features.
      - Dense skip connections between intermediate feature maps at different depths.
      - Bilinear interpolation used for upsampling.
      - Dropout applied in the bottleneck for regularization.

    Args:
        n_channels (int): Number of input channels.
        n_class (int): Number of output classes for segmentation.
    """
    def __init__(self, n_channels, n_class):
        super().__init__()

        # Encoder path with progressively deeper convolutional blocks
        self.enc1 = NestedConvBlock(n_channels, 64)
        self.enc2 = NestedConvBlock(64, 128)
        self.enc3 = NestedConvBlock(128, 256)
        self.enc4 = NestedConvBlock(256, 512)

        # Shared max pooling operation for downsampling
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)

        # First decoder stage with skip connections from encoder
        self.dec3_1 = NestedConvBlock(256 + 512, 256)
        self.dec2_1 = NestedConvBlock(128 + 256, 128)
        self.dec1_1 = NestedConvBlock(64 + 128, 64)  # Fixed: concatenate 64 + 128

        # Second level of nested decoders with additional lateral connections
        self.dec3_2 = NestedConvBlock(256 + 128, 128)
        self.dec2_2 = NestedConvBlock(128 + 64, 64)

        # Third level of nested decoder
        self.dec3_3 = NestedConvBlock(128 + 64, 64)

        # Final convolution to map to the number of output classes
        self.final_conv = nn.Conv2d(64, n_class, kernel_size=1)

        # Dropout applied in the bottleneck to prevent overfitting
        self.dropout = nn.Dropout2d(p=0.1)

    def forward(self, x):
        # Save input spatial resolution for final upsampling
        input_size = x.shape[2:]

        # Encoder pathway
        x1_0 = self.enc1(x)                             # (B, 64, H, W)
        x2_0 = self.enc2(self.pool(x1_0))               # (B, 128, H/2, W/2)
        x3_0 = self.enc3(self.pool(x2_0))               # (B, 256, H/4, W/4)
        x4_0 = self.enc4(self.pool(x3_0))               # (B, 512, H/8, W/8)

        # Apply dropout at the bottleneck
        x4_0 = self.dropout(x4_0)

        # First nested decoder stage
        x3_1 = self.dec3_1(torch.cat([
            x3_0,
            F.interpolate(x4_0, scale_factor=2, mode='bilinear', align_corners=True)
        ], dim=1))

        x2_1 = self.dec2_1(torch.cat([
            x2_0,
            F.interpolate(x3_0, scale_factor=2, mode='bilinear', align_corners=True)
        ], dim=1))

        x1_1 = self.dec1_1(torch.cat([
            x1_0,
            F.interpolate(x2_0, scale_factor=2, mode='bilinear', align_corners=True)
        ], dim=1))

        # Second nested decoder stage
        # Upsample x2_1 to match x3_1's spatial size before merging
        x2_1_up = F.interpolate(x2_1, size=x3_1.shape[2:], mode='bilinear', align_corners=True)
        x3_2 = self.dec3_2(torch.cat([x3_1, x2_1_up], dim=1))

        # Upsample x1_1 to match x2_1 before merging
        x1_1_up = F.interpolate(x1_1, size=x2_1.shape[2:], mode='bilinear', align_corners=True)
        x2_2 = self.dec2_2(torch.cat([x2_1, x1_1_up], dim=1))

        # Third nested decoder stage
        # Further refinement by combining features from previous stage
        x2_2_up = F.interpolate(x2_2, size=x3_2.shape[2:], mode='bilinear', align_corners=True)
        x3_3 = self.dec3_3(torch.cat([x3_2, x2_2_up], dim=1))

        # Final classification layer
        out = self.final_conv(x3_3)

        # Upsample the output to match the original input resolution
        out = F.interpolate(out, size=input_size, mode='bilinear', align_corners=True)
        return out

class STARCOPDataset(Dataset):
    def __init__(self, csv_file, preprocessed_dir, transform=None):
        """
        Args:
            csv_file (str): Path to CSV file containing image IDs in column "id".
            preprocessed_dir (str): Directory where preprocessed .npy files are stored.
            transform (callable, optional): Optional transform to be applied on a sample.
        """
        self.df = pd.read_csv(csv_file)
        self.preprocessed_dir = preprocessed_dir
        self.transform = transform

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        image_id = self.df.iloc[idx]['id']
        image_path = os.path.join(self.preprocessed_dir, f"{image_id}_image.npy")
        label_path = os.path.join(self.preprocessed_dir, f"{image_id}_label.npy")

        image = np.load(image_path)
        label = np.load(label_path)

        image_tensor = torch.from_numpy(image).float()
        label_tensor = torch.from_numpy(label).long()

        if self.transform:
            image_tensor, label_tensor = self.transform(image_tensor, label_tensor)

        return image_tensor, label_tensor

def compute_segmentation_metrics(preds, labels, eps=1e-6):
    """
    Computes various segmentation metrics for binary segmentation:
      - IoU (Intersection over Union)
      - Dice coefficient
      - False Positive Rate (FPR)

    Args:
        preds (torch.Tensor): Predicted segmentation masks (H x W) with values 0 or 1.
        labels (torch.Tensor): Ground truth masks (H x W) with values 0 or 1.
        eps (float): A small value to avoid division by zero.

    Returns:
        dict: Dictionary with metrics.
    """

    preds = preds.cpu().numpy().astype(np.uint8)
    labels = labels.cpu().numpy().astype(np.uint8)


    TP = np.sum((preds == 1) & (labels == 1))
    FP = np.sum((preds == 1) & (labels == 0))
    FN = np.sum((preds == 0) & (labels == 1))
    TN = np.sum((preds == 0) & (labels == 0))


    iou = TP / (TP + FP + FN + eps)


    dice = (2 * TP) / (2 * TP + FP + FN + eps)


    fpr = FP / (FP + TN + eps)

    return {"IoU": iou, "Dice": dice, "FPR": fpr}

def train_one_epoch(model, dataloader, optimizer, criterion, device):
    """
    Trains the model for one epoch.

    Args:
        model (nn.Module): The neural network model.
        dataloader (DataLoader): The data loader for the training dataset.
        optimizer (Optimizer): The optimizer used for updating model parameters.
        criterion (nn.Module): The loss function.
        device (torch.device): The device (CPU or GPU) to use for training.

    Returns:
        float: The average loss for the epoch.
    """
    model.train()
    running_loss = 0.0
    for images, labels in dataloader:
        images = images.to(device)
        labels = labels.to(device)

        optimizer.zero_grad()
        outputs = model(images)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()

        running_loss += loss.item() * images.size(0)
    epoch_loss = running_loss / len(dataloader.dataset)
    return epoch_loss

def test_one_epoch(model, dataloader,criterion, device):
  """
  Evaluates the model for one epoch on the test dataset.

  Args:
    model (nn.Module): The neural network model.
    dataloader (DataLoader): The data loader for the test dataset.
    criterion (nn.Module): The loss function.
    device (torch.device): The device (CPU or GPU) to use for evaluation.

  Returns:
    float: The average loss for the epoch.
  """
  model.eval()
  running_loss = 0.0
  with torch.no_grad():
    for images, labels in dataloader:
      images = images.to(device)
      labels = labels.to(device)

      outputs = model(images)
      loss = criterion(outputs, labels)

      running_loss += loss.item() * images.size(0)
  epoch_loss = running_loss / len(dataloader.dataset)
  return epoch_loss

def evaluate(model, dataloader, device):
    """
    Evaluates the model on the given dataset and computes segmentation metrics.

    Args:
        model (nn.Module): The model to evaluate.
        dataloader (DataLoader): The data loader for the evaluation dataset.
        device (torch.device): The device (CPU or GPU) to use for evaluation.

    Returns:
        dict: A dictionary containing the average IoU, Dice, and FPR metrics.
    """
    model.eval()
    all_metrics = {"IoU": [], "Dice": [], "FPR": []}
    with torch.no_grad():
        for images, labels in dataloader:
            images = images.to(device)
            labels = labels.to(device)

            outputs = model(images)
            preds = torch.argmax(outputs, dim=1)


            for pred, label in zip(preds, labels):
                metrics = compute_segmentation_metrics(pred, label)
                for key in all_metrics.keys():
                    all_metrics[key].append(metrics[key])


    avg_metrics = {key: np.mean(all_metrics[key]) for key in all_metrics}
    return avg_metrics

class DiceLoss(nn.Module):
    def __init__(self, eps=1e-6):
        super().__init__()
        self.eps = eps

    def forward(self, logits, targets):
        """
        Args:
            logits (torch.Tensor): Raw outputs from the network with shape [B, 2, H, W].
            targets (torch.Tensor): Ground truth masks with shape [B, H, W] (values 0 or 1).
        Returns:
            torch.Tensor: Dice loss.
        """
        # Compute probabilities via softmax
        probs = F.softmax(logits, dim=1)

        plume_probs = probs[:, 1, :, :]
        targets = targets.float()

        # Compute intersection and union per image
        intersection = (plume_probs * targets).sum(dim=(1,2))
        union = plume_probs.sum(dim=(1,2)) + targets.sum(dim=(1,2))
        dice = (2.0 * intersection + self.eps) / (union + self.eps)
        dice_loss = 1 - dice.mean()
        return dice_loss

class CombinedLoss(nn.Module):
    def __init__(self, weight_dice=1.0, weight_ce=1.0, eps=1e-6):
        super().__init__()
        self.dice_loss = DiceLoss(eps)
        self.ce_loss = nn.CrossEntropyLoss()
        self.weight_dice = weight_dice
        self.weight_ce = weight_ce

    def forward(self, logits, targets):
        """
        Args:
            logits (torch.Tensor): Raw model outputs with shape [B, 2, H, W].
            targets (torch.Tensor): Ground truth masks with shape [B, H, W].
        Returns:
            torch.Tensor: Combined loss.
        """
        loss_dice = self.dice_loss(logits, targets)
        loss_ce = self.ce_loss(logits, targets)
        return self.weight_dice * loss_dice + self.weight_ce * loss_ce

def main_train(train_csv, test_csv, train_loader,test_loader,optimizer, model, num_epochs=10, batch_size=2, device='cuda'):
    """
    Trains the UNet Plus Plus model for a specified number of epochs.

    Args:
        train_csv (str): Path to the CSV file containing training data.
        test_csv (str): Path to the CSV file containing test data.
        train_loader (DataLoader): DataLoader for the training dataset.
        test_loader (DataLoader): DataLoader for the test dataset.
        optimizer (Optimizer): The optimizer to use for training.
        model (nn.Module): The UNet Plus model.
        num_epochs (int): The number of training epochs.
        batch_size (int): The batch size for training and testing.
        lr (float): The learning rate.
        device (str): The device to use for training ('cuda' or 'cpu').

    Returns:
        nn.Module: The trained UNet Plus Plus model.
    """
    # Initialize the combined loss function with weights for Dice and Cross-Entropy loss
    criterion = CombinedLoss(weight_dice=1.0, weight_ce=1.0)

    # Create a learning rate scheduler that reduces the learning rate on plateau (when validation loss stops improving)
    scheduler = ReduceLROnPlateau(
      optimizer,
      mode='min',
      factor=0.5,
      patience=5,
      threshold=1e-4,
      verbose=True)

    for epoch in range(num_epochs):
        # Train the model for one epoch and get the training loss
        train_loss = train_one_epoch(model, train_loader, optimizer, criterion, device)
        # Evaluate the model on the test dataset and get segmentation metrics (IoU, Dice, FPR)
        metrics = evaluate(model, test_loader, device)
        # Test the model for one epoch and get the test loss
        test_loss = test_one_epoch(model, test_loader, criterion, device)

        print(f"Epoch {epoch+1}/{num_epochs} - Train Loss: {train_loss:.4f}; Test Loss: {test_loss:.4f} - Metrics: {metrics}")

        # Step the learning rate scheduler based on the test loss
        scheduler.step(test_loss)

        # Save the model's state dictionary every 5 epochs
        if (epoch+1) % 5 == 0:
          model_path = os.path.join(
              "/content/drive/MyDrive/ClimateChange/",
              f"epoch_{epoch+1}_UnetPp_V2.pth"
          )
          torch.save(model.state_dict(), model_path)
          print(model_path, "saved")

    return model

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# Set the learning rate for the optimizer.
lr = 1e-4

# Create an instance of the UNetPlusPlus model.
model = UNetPlusPlus(c_in=9, c_out=2, base_channels=128).to(device)

# Initialize the Adam optimizer for training the model.
optimizer = optim.Adam(model.parameters(), lr=lr)

#Copying the preprocessed training data to colab local environment to improve speed of training
shutil.copytree("/content/drive/MyDrive/ClimateChange/preprocessed/STARCOP_train_easy", "/content/STARCOP_train_easy/")

#Copying the preprocessed test data to colab local environment to improve speed of training
shutil.copytree("/content/drive/MyDrive/ClimateChange/preprocessed/STARCOP_test", "/content/STARCOP_test/")

shutil.copy("/content/drive/MyDrive/ClimateChange/STARCOP_train_easy/train_easy.csv", "/content/train_easy.csv")
shutil.copy("/content/drive/MyDrive/ClimateChange/STARCOP_test/test.csv", "/content/test.csv")

train_csv ="/content/train_easy.csv"
test_csv = "/content/test.csv"
root_dir_train = "/content/STARCOP_train_easy"
root_dir_test = "/content/STARCOP_test"

batch_size = 4 #Defining the number of samples in a batch

train_dataset = STARCOPDataset(csv_file=train_csv, preprocessed_dir=root_dir_train) # Create the dataset for training
test_dataset  = STARCOPDataset(csv_file=test_csv, preprocessed_dir=root_dir_test)  # Create the dataset for testing

train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=8) #Create a dataloder for training
test_loader  = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, num_workers=8) #Create a dataloader for testing

#Define device for training
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#Start training
trained_model = main_train(train_csv, test_csv, train_loader=train_loader,test_loader=test_loader,optimizer=optimizer, model=model, num_epochs=250, batch_size=batch_size, device=device)

#Visualise segmentation results on a test image
image, label = test_dataset[180] # Get the 180th image and label from the test dataset

image_tensor = image.unsqueeze(0).to(device)
model.eval() # Set the model to evaluation mode

# Perform inference without gradient calculation
with torch.no_grad():
    #Get the model's output for the image
    output = model(image_tensor)
    #Get the predicted class for each pixel
    prediction = torch.argmax(output, dim=1).squeeze(0).cpu()

# Convert label and prediction to numpy arrays for plotting
label_np = label.cpu().numpy()
prediction_np = prediction.numpy()

#Plot the ground truth and prediction
plt.figure(figsize=(10, 5))
plt.subplot(1, 2, 1)
plt.imshow(label_np, cmap="gray")
plt.title("Ground Truth")
plt.axis("off")

plt.subplot(1, 2, 2)
plt.imshow(prediction_np, cmap="gray")
plt.title("Prediction")
plt.axis("off")

plt.show()

def evaluate_plume_metrics(model, dataloader, device):
    """
    Computes F1, FPR, and Captured Plumes metrics across the dataset.

    Args:
        model (nn.Module): Trained binary segmentation model.
        dataloader (DataLoader): DataLoader for the test dataset.
        device (torch.device): Device to run inference on (CPU or GPU).

    Returns:
        dict: Metrics with keys "F1", "FPR", "Captured Plumes (%)".
    """
    model.eval()
    eps = 1e-6
    total_tp = 0
    total_fp = 0
    total_fn = 0
    total_tn = 0
    captured_plumes_count = 0
    total_plumes = 0

    with torch.no_grad():
        for images, labels in dataloader:
            images = images.to(device)
            labels = labels.to(device)
            outputs = model(images)
            preds = torch.argmax(outputs, dim=1)

            preds_np = preds.cpu().numpy().astype(np.uint8)
            labels_np = labels.cpu().numpy().astype(np.uint8)

            # Process each sample in the batch
            for b in range(preds_np.shape[0]):
                pred_b = preds_np[b]
                label_b = labels_np[b]

                # Calculate confusion matrix components
                tp = np.sum((pred_b == 1) & (label_b == 1)) #True positive
                fp = np.sum((pred_b == 1) & (label_b == 0)) #False positive
                fn = np.sum((pred_b == 0) & (label_b == 1)) #False negative
                tn = np.sum((pred_b == 0) & (label_b == 0)) #True negative
                total_tp += tp
                total_fp += fp
                total_fn += fn
                total_tn += tn

                # Identify distinct plumes in the ground truth
                labeled_plumes, num_plumes = ndimage.label(label_b)
                total_plumes += num_plumes
                # For each plume, if any pixel in the plume is predicted as plume, count it as captured
                for pid in range(1, num_plumes + 1):
                    plume_mask = (labeled_plumes == pid)
                    if np.any(pred_b[plume_mask] == 1):
                        captured_plumes_count += 1

    F1 = (2.0 * total_tp) / (2.0 * total_tp + total_fp + total_fn + eps)
    FPR = total_fp / (total_fp + total_tn + eps)
    captured_plumes_percent = (captured_plumes_count / (total_plumes + eps)) * 100.0

    return {"F1": F1, "FPR": FPR, "Captured Plumes (%)": captured_plumes_percent}

# CSV path and preprocessed directory path
csv_path = "/content/test.csv"
preprocessed_dir = "/content/STARCOP_test"

#Filter the test set based on difficulty column
test_easy_df = test_df[test_df['difficulty'] == 'easy']
test_hard_df = test_df[test_df['difficulty'] == 'hard']

test_df.to_csv("/tmp/test_filtered.csv", index=False)
test_easy_df.to_csv("/tmp/test_easy.csv", index=False)
test_hard_df.to_csv("/tmp/test_hard.csv", index=False)

# Create dataset objects using the preprocessed dataset class
test_dataset = STARCOPDataset(csv_file=csv_path, preprocessed_dir=preprocessed_dir)
test_easy_dataset = STARCOPDataset(csv_file="/tmp/test_easy.csv", preprocessed_dir=preprocessed_dir)
test_hard_dataset = STARCOPDataset(csv_file="/tmp/test_hard.csv", preprocessed_dir=preprocessed_dir)

# Create DataLoaders
test_loader = DataLoader(test_dataset, batch_size=1, shuffle=False)
test_easy_loader = DataLoader(test_easy_dataset, batch_size=1, shuffle=False)
test_hard_loader = DataLoader(test_hard_dataset, batch_size=1, shuffle=False)

# Evaluate metrics on overall test set, and on easy/hard subsets
overall_metrics = evaluate_plume_metrics(model, test_loader, device)
easy_metrics = evaluate_plume_metrics(model, test_easy_loader, device)
hard_metrics = evaluate_plume_metrics(model, test_hard_loader, device)

print("Overall Test Metrics:")
print(overall_metrics)
print("\nEasy Subset Metrics:")
print(easy_metrics)
print("\nHard Subset Metrics:")
print(hard_metrics)