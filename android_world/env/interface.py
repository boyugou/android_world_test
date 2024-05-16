# Copyright 2024 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Environment interface for real-time interaction Android."""

import abc
import dataclasses
import time
from typing import Any, Optional, Self

from android_env import env_interface
from android_env.components import action_type
from android_world.env import actuation
from android_world.env import adb_utils
from android_world.env import json_action
from android_world.env import representation_utils
from android_world.env import ui_tree_wrapper
import dm_env
import numpy as np


def _get_no_op_action() -> dict[str, Any]:
  """Creates a no-op action; used to retrieve screen & UI tree."""
  return {
      'action_type': np.array(action_type.ActionType.LIFT, dtype=np.int32),
      'touch_position': np.array((0.0, 0.0)),
  }


@dataclasses.dataclass(frozen=True)
class State:
  """State of the Android environment.

  Attributes:
    pixels: RGB array of current screen.
    forest: Raw UI forest; see ui_tree_wrapper.py for more info.
    ui_elements: Processed children and stateful UI elements extracted from
      forest.
  """

  pixels: np.ndarray
  forest: Any
  ui_elements: list[representation_utils.UIElement]

  @classmethod
  def create_and_infer_elements(
      cls,
      pixels: np.ndarray,
      forest: Any,
      screen_size: Optional[tuple[int, int]] = None,
  ) -> Self:
    """Creates a new instance, inferring UI elements from the forest."""

    elements = representation_utils.forest_to_ui_elements(
        forest, screen_size=screen_size
    )
    return cls(pixels, forest, elements)


class AsyncEnv(abc.ABC):
  """Interface for interacting with a real-time Android device.

  Computing environments, such as Android, run in real-time, independently of
  the agent interacting with it. All observations and actions are asynchronous
  and OS does not pause when providing observations or when accepting actions.
  Changes from action execution may take some time to appear.
  """

  @property
  @abc.abstractmethod
  def base_env(self) -> env_interface.AndroidEnvInterface:
    """Returns the base Android environment."""

  @abc.abstractmethod
  def reset(self, go_home: bool = False) -> State:
    """Go home on reset.

    Args:
      go_home: Whether to go home during the reset.
    """

  @abc.abstractmethod
  def get_state(self, wait_to_stabilize: bool = False) -> State:
    """Gets the state of the environment; i.e., screenshot & UI tree.

    In practice this will usually be called after executing an action. Logic
    should be implemented, perhaps a simple time.sleep, to ensure the
    environment updates after the action.

    Args:
      wait_to_stabilize: Whether to wait for the screen to stabilize before
        returning state.

    Returns:
      Observation containing RGB array of screen, the accessibility forest,
        and UI elements derived from the forest. See ui_tree_wrapper.py for
        more detail.
    """

  def display_message(self, message: str, header: str = '') -> None:
    """Displays a message on the screen."""

  @abc.abstractmethod
  def execute_action(self, action: json_action.JSONAction) -> None:
    """Executes action on the environment."""

  @property
  @abc.abstractmethod
  def foreground_activity_name(self) -> str:
    """Returns the activity name of the app currently opened in foreground."""

  @property
  @abc.abstractmethod
  def device_screen_size(self) -> tuple[int, int]:
    """Returns the screen size of the environment in pixels: (width, height)."""

  @property
  @abc.abstractmethod
  def logical_screen_size(self) -> tuple[int, int]:
    """Retrieves the logical screen size of the Android device.

    While the physical size is a fixed attribute of the display, the logical
    size is flexible and varies based on system settings such as the orientation
    or if the resolution is changed.

    Returns: The (width, height) in pixels, denoting the logical dimensions of
    the screen. Width and height values are aligned with the device's current
    orientation, meaning width is always logical horizontal direction (like in
    the landscape orientation width will be the physical vertical direction).
    """

  @abc.abstractmethod
  def close(self) -> None:
    """Closes the environment."""

  @property
  @abc.abstractmethod
  def interaction_cache(self) -> str:
    """Returns the interaction cache of the environment."""

  @abc.abstractmethod
  def hide_automation_ui(self) -> None:
    """Hides any UI, such as screen coordinates,."""


def _process_timestep(timestep: dm_env.TimeStep) -> State:
  """Parses timestep observation and returns State."""
  return State(
      pixels=timestep.observation['pixels'],
      forest=timestep.observation[ui_tree_wrapper.OBSERVATION_KEY_FOREST],
      ui_elements=timestep.observation[
          ui_tree_wrapper.OBSERVATION_KEY_UI_ELEMENTS
      ],
  )


class AsyncAndroidEnv(AsyncEnv):
  """Async environment interface using AndroidEnv to communicate with device."""
  interaction_cache = ''

  def __init__(self, base_env: ui_tree_wrapper.UITreeWrapper):
    self._base_env = base_env
    self._prior_state = None
    # Variable used to temporarily save interactions between agent and user.
    # Like when agent use answer action to answer user questions, we
    # use this to save the agent response. Or later on when agent has the
    # ability to ask user question, user's answer will be saved here as well.
    self.interaction_cache = ''

  @property
  def base_env(self) -> env_interface.AndroidEnvInterface:
    return self._base_env

  def reset(self, go_home: bool = False) -> State:
    if go_home:
      adb_utils.press_home_button(self._base_env)
    self.interaction_cache = ''

    return _process_timestep(self._base_env.reset())

  def _get_state(self):
    return _process_timestep(self._base_env.step(_get_no_op_action()))

  def _get_stable_state(
      self,
      stability_threshold: int = 3,
      sleep_duration: float = 0.5,
      timeout: float = 6.0,
  ) -> State:
    """Checks if the UI elements remain stable over a number of checks and gets state.

    Args:
        stability_threshold: Number of consecutive checks where UI elements must
          remain the same to consider UI stable.
        sleep_duration: Time in seconds to wait between checks.
        timeout: Maximum time in seconds to wait for UI to become stable before
          giving up.

    Returns:
        True if UI is considered stable, False if it never stabilizes within the
        timeout.
    """
    if not self._prior_state:
      self._prior_state = self._get_state()

    stable_checks = 0
    elapsed_time = 0.0
    current_state = self._get_state()

    while stable_checks < stability_threshold and elapsed_time < timeout:
      if self._prior_state.ui_elements == current_state.ui_elements:
        stable_checks += 1
        if stable_checks == stability_threshold:
          break  # Exit early if stability is achieved.
      else:
        stable_checks = 0  # Reset if any change is detected
        self._prior_state = current_state

      time.sleep(sleep_duration)
      elapsed_time += sleep_duration
      current_state = self._get_state()

    return current_state

  def get_state(self, wait_to_stabilize: bool = False) -> State:
    if wait_to_stabilize:
      return self._get_stable_state()
    return self._get_state()

  def execute_action(self, action: json_action.JSONAction) -> None:
    if action.action_type == json_action.ANSWER:
      self.interaction_cache = action.text
      if action.text:
        self.display_message(action.text, header='Agent answered:')
      return
    state = self.get_state(wait_to_stabilize=False)
    actuation.execute_adb_action(
        action,
        state.ui_elements,
        self.logical_screen_size,
        self._base_env,
    )

  def hide_automation_ui(self) -> None:
    """Hides the coordinates on screen."""
    adb_utils.issue_generic_request(
        'shell settings put system pointer_location 0', self._base_env
    )

  def display_message(self, message: str, header: str = '') -> None:
    adb_utils.send_android_intent(
        command='broadcast',
        action='com.example.ACTION_UPDATE_OVERLAY',
        env=self._base_env,
        extras={'task_type_string': header, 'goal_string': message},
    )

  @property
  def foreground_activity_name(self) -> str:
    activity = adb_utils.get_current_activity(self._base_env)[0]
    if activity:
      return activity
    else:
      return ''

  @property
  def device_screen_size(self) -> tuple[int, int]:
    return self._base_env.device_screen_size

  @property
  def logical_screen_size(self) -> tuple[int, int]:
    return adb_utils.get_logical_screen_size(self._base_env)

  def close(self) -> None:
    return self._base_env.close()
