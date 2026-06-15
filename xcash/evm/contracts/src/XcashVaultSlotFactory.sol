// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {XcashVaultSlot} from "./XcashVaultSlot.sol";

/// @title XcashVaultSlotFactory
/// @notice Deploys XcashVaultSlot addresses with immutable vault args at deterministic CREATE2 addresses.
contract XcashVaultSlotFactory {
    error InvalidVaultSlotImplementation();
    error ZeroVault();

    event XcashVaultSlotDeployed(
        address indexed vaultSlot,
        address indexed vault,
        bytes32 indexed salt,
        uint256 initialNativeBalance
    );

    address public immutable vaultSlotImplementation;

    constructor(address vaultSlotImplementation_) {
        if (vaultSlotImplementation_.codehash != keccak256(type(XcashVaultSlot).runtimeCode)) {
            revert InvalidVaultSlotImplementation();
        }
        vaultSlotImplementation = vaultSlotImplementation_;
    }

    function deployVaultSlot(address payable vault, bytes32 salt)
        external
        returns (address vaultSlot)
    {
        if (vault == address(0)) revert ZeroVault();
        vaultSlot = Clones.cloneDeterministicWithImmutableArgs(
            vaultSlotImplementation, abi.encodePacked(vault), salt
        );
        emit XcashVaultSlotDeployed(vaultSlot, vault, salt, vaultSlot.balance);
    }
}
