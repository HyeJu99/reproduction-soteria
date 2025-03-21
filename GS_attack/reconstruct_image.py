"""Run reconstruction in a terminal prompt.

Optional arguments can be found in inversefed/options.py
"""

import torch
import torchvision

import numpy as np
from PIL import Image

import inversefed

torch.backends.cudnn.benchmark = inversefed.consts.BENCHMARK

from collections import defaultdict
import datetime
import time
import os


# Parse input arguments
args = inversefed.options().parse_args()
# Parse training strategy
defs = inversefed.training_strategy("conservative")
defs.epochs = args.epochs
# 100% reproducibility?
if args.deterministic:
    image2graph2vec.utils.set_deterministic()


if __name__ == "__main__":
    # Choose GPU device and print status information:
    setup = inversefed.utils.system_startup(args)
    start_time = time.time()

    # Prepare for training

    # Get data:
    loss_fn, trainloader, validloader = inversefed.construct_dataloaders(
        args.dataset, defs, data_path=args.data_path
    )

    dm = torch.as_tensor(
        getattr(inversefed.consts, f"{args.dataset.lower()}_mean"), **setup
    )[:, None, None]
    ds = torch.as_tensor(
        getattr(inversefed.consts, f"{args.dataset.lower()}_std"), **setup
    )[:, None, None]

    if args.dataset == "ImageNet":
        if args.model == "ResNet152":
            model = torchvision.models.resnet152(pretrained=args.trained_model)
        else:
            model = torchvision.models.resnet18(pretrained=args.trained_model)
        model_seed = None
    else:
        model, model_seed = inversefed.construct_model(
            args.model, num_classes=10, num_channels=3
        )
    model.to(**setup)
    model.eval()

    # Sanity check: Validate model accuracy
    training_stats = defaultdict(list)
    # inversefed.training.training_routine.validate(model, loss_fn, validloader, defs, setup, training_stats)
    # name, format = loss_fn.metric()
    # print(f'Val loss is {training_stats["valid_losses"][-1]:6.4f}, Val {name}: {training_stats["valid_" + name][-1]:{format}}.')

    # Choose example images from the validation set or from third-party sources
    if args.target_id == -1:  # demo image
        # Specify PIL filter for lower pillow versions
        ground_truth = torch.as_tensor(
            np.array(Image.open("auto.jpg").resize((32, 32), Image.BICUBIC)) / 255,
            **setup,
        )
        ground_truth = (
            ground_truth.permute(2, 0, 1).sub(dm).div(ds).unsqueeze(0).contiguous()
        )
        if not args.label_flip:
            labels = torch.as_tensor((1,), device=setup["device"])
        else:
            labels = torch.as_tensor((5,), device=setup["device"])
        target_id = -1
        print("ground_truth.shape:", ground_truth.shape)
    else:
        if args.target_id is None:
            target_id = np.random.randint(len(validloader.dataset))
        else:
            target_id = args.target_id
        ground_truth, labels = validloader.dataset[target_id]
        if args.label_flip:
            labels = torch.randint((10,))
        ground_truth, labels = ground_truth.unsqueeze(0).to(**setup), torch.as_tensor(
            (labels,), device=setup["device"]
        )
        print("ground_truth.shape:", ground_truth.shape)
    img_shape = (3, ground_truth.shape[2], ground_truth.shape[3])

    # Run reconstruction
    model.zero_grad()
    ground_truth.requires_grad = True
    target_loss, _, _ = loss_fn(model(ground_truth), labels)

    # ================ Method ===============
    if args.model in ["LeNet", "ConvNet"] and args.defense == "ours":
        print("applying our defense strategy...")
        feature_fc1_graph = model.extract_feature()  # [1, 2304]
        deviation_f1_target = torch.zeros_like(feature_fc1_graph)
        deviation_f1_x_norm = torch.zeros_like(feature_fc1_graph)
        for f in range(deviation_f1_x_norm.size(1)):  # [1, 2304]
            deviation_f1_target[:, f] = 1
            if f % 500 == 0:
                print("(1)feature_fc1_graph[0][0]", feature_fc1_graph[0][0])

            feature_fc1_graph.backward(deviation_f1_target, retain_graph=True)
            if f % 500 == 0:
                print("(2)feature_fc1_graph[0][0]", feature_fc1_graph[0][0])

            deviation_f1_x = ground_truth.grad.data  # [1,3,32,32]
            if f % 500 == 0:
                print("deviation_f1_x.shape", deviation_f1_x.shape)
            deviation_f1_x_norm[:, f] = torch.norm(
                deviation_f1_x.view(deviation_f1_x.size(0), -1), dim=1
            ) / (feature_fc1_graph.data[:, f] + 0.1)

            model.zero_grad()
            ground_truth.grad.data.zero_()
            deviation_f1_target[:, f] = 0
        deviation_f1_x_norm_sum = deviation_f1_x_norm.sum(axis=0)  # [2304]
        print("deviation_f1_x_norm_sum.shape", deviation_f1_x_norm_sum.shape)
        thresh = np.percentile(
            deviation_f1_x_norm_sum.flatten().cpu().numpy(), args.pruning_rate
        )
        mask = np.where(abs(deviation_f1_x_norm_sum.cpu()) < thresh, 0, 1).astype(
            np.float32
        )
    # ================ ====== ===============

    input_gradient = torch.autograd.grad(target_loss, model.parameters())
    input_gradient = [grad.detach() for grad in input_gradient]

    # ================ Method ===============
    if args.model == "LeNet" and args.defense == "ours":
        input_gradient[8] = input_gradient[8] * torch.Tensor(mask).to(**setup)
    elif args.model == "ConvNet" and args.defense == "ours":
        input_gradient[-2] = input_gradient[-2] * torch.Tensor(mask).to(**setup)
    if args.defense == "prune":
        for i in range(len(input_gradient)):
            grad_tensor = input_gradient[i].cpu().numpy()
            flattened_weights = np.abs(grad_tensor.flatten())
            # Generate the pruning threshold according to 'prune by percentage'. (Your code: 1 Line)
            thresh = np.percentile(flattened_weights, args.pruning_rate)
            grad_tensor = np.where(abs(grad_tensor) < thresh, 0, grad_tensor)
            input_gradient[i] = torch.Tensor(grad_tensor).to(**setup)
    full_norm = torch.stack([g.norm() for g in input_gradient]).mean()
    print(f"Full gradient norm is {full_norm:e}.")
    # ================ ====== ===============

    config = dict(
        signed=args.signed,
        boxed=args.boxed,
        cost_fn=args.cost_fn,
        indices="def",
        weights="equal",
        lr=0.1,
        optim=args.optimizer,
        restarts=args.restarts,
        max_iterations=2_000,  # 24_000,
        total_variation=args.tv,
        init="randn",
        filter="none",
        lr_decay=True,
        scoring_choice="loss",
    )
    rec_machine = inversefed.GradientReconstructor(
        model, (dm, ds), config, num_images=args.num_images
    )
    output, stats = rec_machine.reconstruct(
        input_gradient, labels, img_shape=img_shape, dryrun=args.dryrun
    )

    # Compute stats
    test_mse = (output - ground_truth).pow(2).mean().item()  # higher
    test_psnr = inversefed.metrics.psnr(output, ground_truth, factor=1 / ds)  # higher
    test_ssim = inversefed.metrics.ssim(output, ground_truth)  # higher
    feat_mse = (model(output) - model(ground_truth)).pow(2).mean().item()  # lower

    # Save the resulting image
    if args.save_image and not args.dryrun:
        os.makedirs(args.image_path, exist_ok=True)
        output_denormalized = torch.clamp(output * ds + dm, 0, 1)
        rec_filename = (
            f'{validloader.dataset.classes[labels][0]}_{"trained" if args.trained_model else ""}'
            f"{args.model}_{args.cost_fn}-{args.target_id}-{args.defense}-{args.pruning_rate}.png"
        )
        torchvision.utils.save_image(
            output_denormalized, os.path.join(args.image_path, rec_filename)
        )

        gt_denormalized = torch.clamp(ground_truth * ds + dm, 0, 1)
        gt_filename = f"{validloader.dataset.classes[labels][0]}_ground_truth-{args.target_id}.png"
        torchvision.utils.save_image(
            gt_denormalized, os.path.join(args.image_path, gt_filename)
        )
    else:
        rec_filename = None
        gt_filename = None

    # Save to a table:
    print(
        f"Rec. loss: {stats['opt']:2.4f} | MSE: {test_mse:2.4f} | PSNR: {test_psnr:4.2f} | SSIM: {test_ssim:2.4f} | FMSE: {feat_mse:2.4e} |"
    )

    # Print final timestamp
    print(datetime.datetime.now().strftime("%A, %d. %B %Y %I:%M%p"))
    print("---------------------------------------------------")
    print(
        f"Finished computations with time: {str(datetime.timedelta(seconds=time.time() - start_time))}"
    )
    print("-------------Job finished.-------------------------")
