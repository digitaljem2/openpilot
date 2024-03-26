from openpilot.common.numpy_fast import clip
from opendbc.can.packer import CANPacker
from openpilot.selfdrive.car import apply_std_steer_angle_limits
from openpilot.selfdrive.car.interfaces import CarControllerBase
from openpilot.selfdrive.car.tesla.teslacan import TeslaCAN
from openpilot.selfdrive.car.tesla.values import DBC, CANBUS, CarControllerParams, CAR


class CarController(CarControllerBase):
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.frame = 0
    self.apply_angle_last = 0
    self.packer = CANPacker(dbc_name)
    self.pt_packer = CANPacker(DBC[CP.carFingerprint]['pt'])
    self.tesla_can = TeslaCAN(self.packer, self.pt_packer)
    self.model3 = CP.carFingerprint == CAR.AP3_MODEL3
    self.last_das_control_counter = -1

  def update(self, CC, CS, now_nanos):
    actuators = CC.actuators
    pcm_cancel_cmd = CC.cruiseControl.cancel

    can_sends = []

    # Temp disable steering on a hands_on_fault, and allow for user override
    hands_on_fault = CS.steer_warning == "EAC_ERROR_HANDS_ON" and CS.hands_on_level >= 3
    lkas_enabled = CC.latActive and not hands_on_fault

    if self.frame % 2 == 0:
      if lkas_enabled:
        # Angular rate limit based on speed
        apply_angle = apply_std_steer_angle_limits(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgo, CarControllerParams)

        # To not fault the EPS
        apply_angle = clip(apply_angle, CS.out.steeringAngleDeg - 20, CS.out.steeringAngleDeg + 20)
      else:
        apply_angle = CS.out.steeringAngleDeg

      self.apply_angle_last = apply_angle
      can_sends.append(self.tesla_can.create_steering_control(apply_angle, lkas_enabled, (self.frame // 2) % 16))

    # Longitudinal control (in sync with stock message, about 40Hz)
    if self.CP.openpilotLongitudinalControl:
      acc_state = CS.das_control["DAS_accState"]
      target_accel = actuators.accel
      target_speed = max(CS.out.vEgo + (target_accel * CarControllerParams.ACCEL_TO_SPEED_MULTIPLIER), 0)
      max_accel = 0 if target_accel < 0 else target_accel
      min_accel = 0 if target_accel > 0 else target_accel

      if self.model3:
        counter = (CS.das_control["DAS_controlCounter"] + 1) % 8
        if counter != self.last_das_control_counter or True:
          self.last_das_control_counter = counter
          can_sends.extend(self.tesla_can.create_longitudinal_commands(acc_state, target_speed, min_accel, max_accel, counter))
      else:
        while len(CS.das_control_counters) > 0:
          can_sends.extend(self.tesla_can.create_longitudinal_commands(acc_state, target_speed, min_accel, max_accel, CS.das_control_counters.popleft()))

    # Cancel on user steering override, since there is no steering torque blending
    if hands_on_fault:
      pcm_cancel_cmd = True

    if self.frame % 10 == 0 and pcm_cancel_cmd:
      if self.model3 and CS.cruiseState.enabled:
        can_sends.append(self.tesla_can.model3_cancel_acc(CS.sccm_right_stalk))
      else:
        # Spam every possible counter value, otherwise it might not be accepted
        for counter in range(16):
          can_sends.append(self.tesla_can.create_action_request(CS.msg_stw_actn_req, pcm_cancel_cmd, CANBUS.chassis, counter))
          can_sends.append(self.tesla_can.create_action_request(CS.msg_stw_actn_req, pcm_cancel_cmd, CANBUS.autopilot_chassis, counter))

    # TODO: HUD control

    new_actuators = actuators.copy()
    new_actuators.steeringAngleDeg = self.apply_angle_last

    self.frame += 1
    return new_actuators, can_sends
